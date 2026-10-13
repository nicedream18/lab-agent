"""预约时间工具与冲突判定：_to_minutes / _to_hhmm / _overlap / has_conflict。

这是整个预约链路里最纯、最便宜、也最容易写错的一层：

- 模型抽出来的时间可能是 "9:00" 而不是 "08:00"，所以要容错解析；
- 冲突判定用的是 [start, end) 左闭右开，`<=` / `>=` 会把「上一场 12:00 结束、
  下一场 12:00 开始」误判成冲突 —— 这个边界正是最容易退化的一行。

以前这些函数只被工具层间接带到，没有任何直接用例。
"""

import unittest

from evals.offline_env import configure_offline
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

configure_offline()

import app.models  # noqa: F401
from app.common.exceptions import BusinessException
from app.database import Base
from app.models.equipment import Equipment
from app.models.lab import Lab
from app.models.reservation import Reservation
from app.models.user import User
from app.services import reservation_service as service


class TimeParsingCases(unittest.TestCase):
    def test_accepts_both_padded_and_unpadded_hours(self):
        self.assertEqual(service._to_minutes("9:30"), 570)
        self.assertEqual(service._to_minutes("09:30"), 570)
        self.assertEqual(service._to_minutes("00:00"), 0)
        self.assertEqual(service._to_minutes("23:59"), 1439)

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(service._to_minutes(" 14:00 "), 840)

    def test_malformed_input_raises_business_exception(self):
        for bad in ("", "930", "ab:cd", "9:", ":30", "9:xx"):
            with self.subTest(bad=bad):
                with self.assertRaises(BusinessException):
                    service._to_minutes(bad)

    def test_formatting_is_zero_padded_and_round_trips(self):
        self.assertEqual(service._to_hhmm(0), "00:00")
        self.assertEqual(service._to_hhmm(570), "09:30")
        self.assertEqual(service._to_hhmm(1439), "23:59")
        self.assertEqual(service._to_hhmm(service._to_minutes("9:05")), "09:05")


class OverlapCases(unittest.TestCase):
    def test_overlapping_ranges_conflict(self):
        self.assertTrue(service._overlap("14:00", "16:00", "15:00", "17:00"))

    def test_contained_range_conflicts(self):
        self.assertTrue(service._overlap("14:00", "18:00", "15:00", "16:00"))

    def test_identical_range_conflicts(self):
        self.assertTrue(service._overlap("14:00", "16:00", "14:00", "16:00"))

    def test_disjoint_ranges_do_not_conflict(self):
        self.assertFalse(service._overlap("08:00", "10:00", "14:00", "16:00"))

    def test_touching_endpoints_do_not_conflict(self):
        """回归：左闭右开 —— 上一场 12:00 结束、下一场 12:00 开始不算冲突。"""
        self.assertFalse(service._overlap("10:00", "12:00", "12:00", "14:00"))
        self.assertFalse(service._overlap("12:00", "14:00", "10:00", "12:00"))

    def test_unpadded_input_is_normalised(self):
        self.assertTrue(service._overlap("9:00", "11:00", "10:00", "12:00"))


class ConflictCases(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.addCleanup(engine.dispose)
        self.db = Session(engine)
        self.addCleanup(self.db.close)

        self.user = User(
            username="student", password="hash", name="学生", role="student"
        )
        self.lab = Lab(
            name="计算机实验室", open_time="08:00", close_time="20:00", status=1
        )
        self.db.add_all([self.user, self.lab])
        self.db.commit()
        self.equipment = Equipment(
            lab_id=self.lab.id, name="开发主机", status=1, quantity=10
        )
        self.db.add(self.equipment)
        self.db.commit()

    def book(
        self, start, end, *, status=0, equipment_id=None, lab=None, date="2099-10-13"
    ):
        item = Reservation(
            user_id=self.user.id,
            lab_id=(lab or self.lab).id,
            equipment_id=equipment_id,
            date=date,
            start_time=start,
            end_time=end,
            status=status,
        )
        self.db.add(item)
        self.db.commit()
        return item

    def test_busy_slots_only_count_pending_and_approved(self):
        self.book("08:00", "10:00", status=0)
        self.book("10:00", "12:00", status=1)
        self.book("12:00", "14:00", status=2)
        self.book("14:00", "16:00", status=3)
        busy = service.list_busy_slots(self.db, self.lab.id, "2099-10-13")
        self.assertEqual([slot["start_time"] for slot in busy], ["08:00", "10:00"])

    def test_lab_wide_view_ignores_equipment_bound_rows(self):
        self.book("08:00", "10:00")
        self.book("10:00", "12:00", equipment_id=self.equipment.id)
        self.assertEqual(
            len(service.list_busy_slots(self.db, self.lab.id, "2099-10-13")), 1
        )
        self.assertEqual(
            len(
                service.list_busy_slots(
                    self.db, self.lab.id, "2099-10-13", self.equipment.id
                )
            ),
            1,
        )

    def test_other_days_do_not_interfere(self):
        self.book("08:00", "10:00", date="2099-10-14")
        self.assertEqual(
            service.list_busy_slots(self.db, self.lab.id, "2099-10-13"), []
        )

    def test_has_conflict_detects_overlap_and_free_slots(self):
        self.book("14:00", "16:00")
        self.assertTrue(
            service.has_conflict(self.db, self.lab.id, "2099-10-13", "15:00", "17:00")
        )
        self.assertFalse(
            service.has_conflict(self.db, self.lab.id, "2099-10-13", "16:00", "18:00")
        )
        self.assertFalse(
            service.has_conflict(self.db, self.lab.id, "2099-10-13", "09:00", "10:00")
        )

    def test_cancelled_reservation_frees_the_slot(self):
        self.book("14:00", "16:00", status=3)
        self.assertFalse(
            service.has_conflict(self.db, self.lab.id, "2099-10-13", "15:00", "17:00")
        )

    def test_equipment_conflicts_are_tracked_per_equipment(self):
        self.book("14:00", "16:00", equipment_id=self.equipment.id)
        self.assertTrue(
            service.has_conflict(
                self.db,
                self.lab.id,
                "2099-10-13",
                "15:00",
                "16:00",
                self.equipment.id,
            )
        )
        # 实验室整体视角下，绑设备的记录不算占用
        self.assertFalse(
            service.has_conflict(self.db, self.lab.id, "2099-10-13", "15:00", "16:00")
        )


if __name__ == "__main__":
    unittest.main()
