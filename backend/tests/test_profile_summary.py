"""用户画像与记忆：_profile_summary、load_memory_context、remember、短期记忆。

_profile_summary 是「把预约记录聚合成一句事实」的地方，也是最容易说出假话的
地方：只约过 1 次就写「最常用实验室」，模型会当成事实转述给用户。所以这里的
重点不是「能算出来」，而是 **判据的边界**：

- 1 次不算「最常用」（下限 2）
- 并列不算「最常用」（严格多于第二名）
- 已取消的记录不算（status 3）
- 别人的记录不算

同时钉住 load_memory_context 的降级行为：画像聚合失败不能连带把明细偏好也吞掉。
这些用例全部跑在内存 SQLite 上，不碰 MySQL。
"""

import unittest
from collections import OrderedDict
from unittest.mock import patch

from evals.offline_env import configure_offline
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

configure_offline()

import app.models  # noqa: F401
from app.agent import memory
from app.database import Base
from app.models.lab import Lab
from app.models.reservation import Reservation
from app.models.user import User
from app.models.user_memory import UserMemory


class ProfileSummaryCases(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = Session(engine)
        self.addCleanup(self.db.close)

        self.user = User(
            username="student", password="hash", name="学生", role="student"
        )
        self.other = User(
            username="other", password="hash", name="其他人", role="student"
        )
        self.computer = Lab(
            name="计算机实验室", open_time="08:00", close_time="20:00", status=1
        )
        self.physics = Lab(
            name="物理实验室", open_time="08:00", close_time="20:00", status=1
        )
        self.db.add_all([self.user, self.other, self.computer, self.physics])
        self.db.commit()

    def book(self, lab, when, *, status=0, user=None):
        start, end = when
        item = Reservation(
            user_id=(user or self.user).id,
            lab_id=lab.id,
            date="2099-10-13",
            start_time=start,
            end_time=end,
            status=status,
        )
        self.db.add(item)
        self.db.commit()
        return item

    def test_no_records_at_all_is_empty(self):
        self.assertEqual(memory._profile_summary(self.db, self.user.id), "")

    def test_a_single_booking_is_not_a_favourite(self):
        """只有 1 条记录时，既不能说「最常用实验室」，也不能说「习惯时段」。"""
        self.book(self.computer, ("14:00", "16:00"))
        self.assertEqual(memory._profile_summary(self.db, self.user.id), "")

    def test_two_same_bookings_yield_lab_and_period(self):
        self.book(self.computer, ("14:00", "16:00"))
        self.book(self.computer, ("14:00", "17:00"))
        summary = memory._profile_summary(self.db, self.user.id)
        self.assertIn("## 用户画像（来自 2 条真实预约记录）", summary)
        self.assertIn("- 最常用实验室：计算机实验室（2 次）", summary)
        self.assertIn("- 习惯时段：下午（2 次）", summary)
        self.assertIn("14:00", summary)

    def test_tied_labs_are_not_a_favourite(self):
        """两个实验室各 2 次 → 并列，不能说其中一个是「最常用」。"""
        for when in [("09:00", "10:00"), ("09:00", "11:00")]:
            self.book(self.computer, when)
            self.book(self.physics, when)
        summary = memory._profile_summary(self.db, self.user.id)
        self.assertNotIn("最常用实验室", summary)
        self.assertIn("- 习惯时段：上午（4 次）", summary)

    def test_cancelled_records_are_excluded(self):
        for _ in range(3):
            self.book(self.computer, ("14:00", "16:00"), status=3)
        self.assertEqual(memory._profile_summary(self.db, self.user.id), "")

    def test_rejected_records_still_count(self):
        """被拒绝代表「本来想约」，仍算数（只有取消不算）。"""
        self.book(self.computer, ("14:00", "16:00"), status=2)
        self.book(self.computer, ("14:00", "17:00"), status=2)
        summary = memory._profile_summary(self.db, self.user.id)
        self.assertIn("- 最常用实验室：计算机实验室（2 次）", summary)

    def test_other_users_records_are_excluded(self):
        for when in [("14:00", "16:00"), ("14:00", "17:00")]:
            self.book(self.computer, when, user=self.other)
        self.assertEqual(memory._profile_summary(self.db, self.user.id), "")

    def test_unparsable_start_time_is_skipped_not_fatal(self):
        self.book(self.computer, ("14:00", "16:00"))
        self.book(self.computer, ("坏时间", "17:00"))
        summary = memory._profile_summary(self.db, self.user.id)
        self.assertIn("## 用户画像（来自 2 条真实预约记录）", summary)
        # 只有一个可解析的开始时间，凑不满「至少 2 次」，所以不写习惯时段
        self.assertNotIn("习惯时段", summary)


class LoadMemoryContextCases(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = Session(engine)
        self.addCleanup(self.db.close)

        self.user = User(
            username="student", password="hash", name="学生", role="student"
        )
        self.computer = Lab(
            name="计算机实验室", open_time="08:00", close_time="20:00", status=1
        )
        self.db.add_all([self.user, self.computer])
        self.db.commit()

    def test_first_use_placeholder(self):
        self.assertEqual(
            memory.load_memory_context(self.db, self.user.id),
            "（暂无历史偏好记录，这是该用户第一次使用）",
        )

    def test_profile_is_listed_before_preferences(self):
        for when in [("14:00", "16:00"), ("14:00", "17:00")]:
            self.db.add(
                Reservation(
                    user_id=self.user.id,
                    lab_id=self.computer.id,
                    date="2099-10-13",
                    start_time=when[0],
                    end_time=when[1],
                    status=0,
                )
            )
        self.db.commit()
        memory.remember(self.db, self.user.id, "习惯用开发主机")

        context = memory.load_memory_context(self.db, self.user.id)
        self.assertIn("## 用户画像", context)
        self.assertIn("## 已沉淀的偏好", context)
        self.assertLess(
            context.index("## 用户画像"), context.index("## 已沉淀的偏好")
        )
        self.assertIn("- 习惯用开发主机", context)

    def test_reading_preferences_bumps_hit_count(self):
        item = memory.remember(self.db, self.user.id, "喜欢安静的实验室")
        self.assertEqual(item.hit_count, 0)
        memory.load_memory_context(self.db, self.user.id)
        memory.load_memory_context(self.db, self.user.id)
        self.db.refresh(item)
        self.assertEqual(item.hit_count, 2)

    def test_profile_failure_still_returns_preferences(self):
        """画像只是锦上添花，聚合炸了不能连带把明细偏好也吞掉。"""
        memory.remember(self.db, self.user.id, "喜欢安静的实验室")
        with patch.object(memory, "_profile_summary", side_effect=RuntimeError("boom")):
            with self.assertLogs("app.agent.memory", level="ERROR"):
                context = memory.load_memory_context(self.db, self.user.id)
        self.assertNotIn("## 用户画像", context)
        self.assertIn("喜欢安静的实验室", context)


class RememberCases(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = Session(engine)
        self.addCleanup(self.db.close)
        self.user = User(
            username="student", password="hash", name="学生", role="student"
        )
        self.db.add(self.user)
        self.db.commit()

    def test_blank_content_is_ignored(self):
        self.assertIsNone(memory.remember(self.db, self.user.id, "   "))
        self.assertEqual(self.db.query(UserMemory).count(), 0)

    def test_duplicate_content_accumulates_instead_of_inserting(self):
        memory.remember(self.db, self.user.id, "喜欢安静的实验室")
        again = memory.remember(self.db, self.user.id, " 喜欢安静的实验室 ")
        self.assertEqual(self.db.query(UserMemory).count(), 1)
        self.assertEqual(again.hit_count, 1)

    def test_different_content_creates_new_rows(self):
        memory.remember(self.db, self.user.id, "甲")
        memory.remember(self.db, self.user.id, "乙")
        self.assertEqual(self.db.query(UserMemory).count(), 2)


class ShortTermMemoryCases(unittest.TestCase):
    """短期记忆走进程内 OrderedDict，用 patch 换一份干净的，避免用例互相污染。"""

    def isolated(self):
        return patch.object(memory, "_sessions", OrderedDict())

    def test_append_then_get_history(self):
        with self.isolated():
            memory.append_turn("c1", "帮我预约", "好的")
            self.assertEqual(
                memory.get_history("c1"),
                [
                    {"role": "user", "content": "帮我预约"},
                    {"role": "assistant", "content": "好的"},
                ],
            )

    def test_unknown_or_blank_conversation_returns_empty(self):
        with self.isolated():
            self.assertEqual(memory.get_history("nope"), [])
            self.assertEqual(memory.get_history(""), [])
            memory.append_turn("", "问", "答")
            self.assertEqual(memory.get_history(""), [])

    def test_history_keeps_only_the_newest_messages(self):
        with self.isolated(), patch.object(memory, "MAX_HISTORY_MESSAGES", 2):
            memory.append_turn("c1", "第一问", "第一答")
            memory.append_turn("c1", "第二问", "第二答")
            self.assertEqual(
                [item["content"] for item in memory.get_history("c1")],
                ["第二问", "第二答"],
            )

    def test_least_recently_used_session_is_evicted(self):
        with self.isolated(), patch.object(memory, "MAX_SESSIONS", 2):
            memory.append_turn("c1", "问1", "答1")
            memory.append_turn("c2", "问2", "答2")
            memory.get_history("c1")  # c1 变成最近使用
            memory.append_turn("c3", "问3", "答3")
            self.assertEqual(list(memory._sessions), ["c1", "c3"])


if __name__ == "__main__":
    unittest.main()
