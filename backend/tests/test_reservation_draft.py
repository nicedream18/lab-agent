"""预约草稿的跨轮授权及提交边界。"""

import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from evals.offline_env import configure_offline
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

configure_offline()

import app.models  # noqa: F401
from app.agent import graph
from app.agent.state import AgentContext
from app.database import Base
from app.models.equipment import Equipment
from app.models.lab import Lab
from app.models.reservation import Reservation
from app.models.user import User
from app.services import reservation_draft_service as drafts


@contextmanager
def no_trace(*_args, **_kwargs):
    yield {"output": "", "status": "success"}


class DraftCases(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = User(
            username="student", password="hash", name="学生", role="student", status=1
        )
        self.other = User(
            username="other", password="hash", name="其他人", role="student", status=1
        )
        self.lab = Lab(
            name="计算机实验室", open_time="08:00", close_time="20:00", status=1
        )
        self.db.add_all([self.user, self.other, self.lab])
        self.db.commit()
        self.db.add(
            Equipment(lab_id=self.lab.id, name="开发主机", status=1, quantity=20)
        )
        self.db.commit()
        self.ctx = AgentContext(db=self.db, user=self.user, conversation_id="session-a")
        self.target = {
            "lab_name": self.lab.name,
            "equipment_name": "开发主机",
            "date": "2099-10-13",
            "start_time": "08:00",
            "end_time": "10:00",
        }

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def analyze(self, query, slots=None, *, ctx=None):
        state = {"user_query": query, "today": "2099-10-11", "history": []}
        result = {
            "slots": slots or {},
            "missing_slots": [],
            "authorized": False,
            "allowed_write_tools": [],
            "plan_needed": False,
        }
        return graph._prepare_reservation_draft(ctx or self.ctx, state, result)

    def test_read_availability_then_short_confirmation(self):
        graph._save_available_draft(
            self.ctx,
            {
                "allowed_write_tools": [],
                "slots": {"equipment_name": "开发主机"},
                "tool_results": [
                    {
                        "tool": "query_lab_availability",
                        "ok": True,
                        "args": {
                            key: self.target[key]
                            for key in ("lab_name", "date", "start_time", "end_time")
                        },
                        "result": {"requested": {"available": True}},
                    }
                ],
            },
        )
        self.assertFalse(drafts.load(self.db, self.user.id, "session-a").requested)
        confirmed = self.analyze("确认预约")
        self.assertEqual(confirmed["draft_target"], self.target)
        self.assertEqual(confirmed["allowed_write_tools"], ["create_reservation"])
        self.assertNotIn("draft_target", self.analyze("确认"))
        self.assertEqual(self.analyze("确认预约")["draft_target"], self.target)
        other = AgentContext(db=self.db, user=self.other, conversation_id="session-a")
        self.assertNotIn("draft_target", self.analyze("确认预约", ctx=other))

    def test_explicit_booking_then_time_only_reply(self):
        first = self.analyze(
            "帮我预约计算机实验室的开发主机",
            {
                "lab_name": self.lab.name,
                "equipment_name": "开发主机",
            },
        )
        self.assertEqual(
            first["draft_target"],
            {
                "lab_name": self.lab.name,
                "equipment_name": "开发主机",
            },
        )
        self.assertTrue(first["draft_pending"])
        self.assertEqual(first["allowed_write_tools"], [])
        draft = drafts.load(self.db, self.user.id, "session-a")
        self.assertTrue(draft.requested)
        followup = self.analyze(
            "后天上午八点到十点",
            {
                "date": "2099-10-13",
                "start_time": "08:00",
                "end_time": "10:00",
            },
        )
        self.assertEqual(followup["draft_target"], self.target)
        self.assertEqual(followup["allowed_write_tools"], ["create_reservation"])

    def test_new_target_never_inherits_old_time(self):
        drafts.save(self.db, self.user.id, "session-a", self.target, requested=False)
        result = self.analyze(
            "帮我预约明天的物理实验室",
            {
                "lab_name": "物理实验室",
                "date": "2099-10-12",
                "start_time": "08:00",
                "end_time": "10:00",
            },
        )
        self.assertEqual(result["allowed_write_tools"], [])
        target = drafts.load(self.db, self.user.id, "session-a").target
        self.assertEqual(target["lab_name"], "物理实验室")
        self.assertNotIn("start_time", target)
        self.assertNotIn("end_time", target)

    def test_guard_exact_target_and_duplicate(self):
        ready = self.analyze(
            "帮我预约 2099-10-13 08:00-10:00 计算机实验室的开发主机", self.target
        )
        state = {
            "allowed_write_tools": ["create_reservation"],
            "draft_target": self.target,
            "draft_revision": ready["draft_revision"],
            "bound_tools": ["create_reservation"],
            "tool_results": [],
            "plan_cursor": 0,
        }

        def execute(args):
            with (
                patch.object(graph, "trace_node", no_trace),
                patch.object(graph, "write_tool_execution"),
                patch.object(graph.memory, "learn_from_reservation", return_value=None),
            ):
                return graph.node_execute(self.ctx)(
                    {
                        **state,
                        "pending_calls": [
                            {
                                "id": "call-1",
                                "name": "create_reservation",
                                "args": args,
                                "step": 1,
                            }
                        ],
                    }
                )

        self.assertFalse(
            execute({**self.target, "end_time": "11:00"})["tool_results"][0]["ok"]
        )
        self.assertEqual(self.db.query(Reservation).count(), 0)
        self.assertTrue(execute(self.target)["tool_results"][0]["ok"])
        self.assertEqual(self.db.query(Reservation).count(), 1)
        self.assertFalse(execute(self.target)["tool_results"][0]["ok"])
        self.assertEqual(self.db.query(Reservation).count(), 1)
        self.assertIn("已经提交", self.analyze("确认预约")["error"])

    def test_unrelated_turn_expired_and_ambiguous_do_not_authorize(self):
        drafts.save(self.db, self.user.id, "session-a", self.target, requested=False)
        self.assertNotIn("draft_target", self.analyze("有哪些开放实验室？"))
        self.assertIsNone(drafts.load(self.db, self.user.id, "session-a"))

    def test_cancel_discards_pending_booking(self):
        drafts.save(self.db, self.user.id, "session-a", self.target, requested=False)
        self.analyze("帮我取消预约")
        self.assertIsNone(drafts.load(self.db, self.user.id, "session-a"))
        self.assertNotIn("draft_target", self.analyze("确认预约"))

    def test_expired_and_ambiguous_draft_do_not_authorize(self):
        draft = drafts.save(
            self.db, self.user.id, "session-a", self.target, requested=False
        )
        draft.expires_at = datetime.now() - timedelta(seconds=1)
        self.db.commit()
        self.assertNotIn("draft_target", self.analyze("确认预约"))
        graph._save_available_draft(
            self.ctx,
            {
                "allowed_write_tools": [],
                "slots": {},
                "tool_results": [
                    {
                        "tool": "query_lab_availability",
                        "ok": True,
                        "args": {"lab_name": self.lab.name, "date": "2099-10-13"},
                        "result": {"requested": {"available": True}},
                    }
                ],
            },
        )
        self.assertIsNone(drafts.load(self.db, self.user.id, "session-a"))

    def test_in_flight_submission_cannot_be_overwritten(self):
        draft = drafts.save(
            self.db, self.user.id, "session-a", self.target, requested=True
        )
        draft.status = "submitting"
        self.db.commit()
        result = self.analyze(
            "帮我预约 2099-10-13 08:00-10:00 计算机实验室的开发主机", self.target
        )
        self.assertEqual(result["allowed_write_tools"], [])
        self.assertIn("正在提交", result["error"])


if __name__ == "__main__":
    unittest.main()
