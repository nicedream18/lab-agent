"""跨轮取消候选与原子批量取消。"""

import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from evals.offline_env import configure_offline

configure_offline()

import app.models  # noqa: F401
from app.agent import graph
from app.agent.state import AgentContext
from app.database import Base
from app.models.lab import Lab
from app.models.reservation import Reservation
from app.models.user import User
from app.services import cancellation_draft_service as drafts


class CancellationDraftCases(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = User(username="a", password="x", name="A", role="student", status=1)
        self.other = User(username="b", password="x", name="B", role="student", status=1)
        self.lab = Lab(name="计算机实验室", open_time="08:00", close_time="20:00", status=1)
        self.db.add_all([self.user, self.other, self.lab])
        self.db.commit()
        self.one = Reservation(user_id=self.user.id, lab_id=self.lab.id, date="2099-10-13", start_time="08:00", end_time="10:00", status=0)
        self.two = Reservation(user_id=self.user.id, lab_id=self.lab.id, date="2099-10-13", start_time="14:00", end_time="17:00", status=0)
        self.foreign = Reservation(user_id=self.other.id, lab_id=self.lab.id, date="2099-10-13", start_time="18:00", end_time="20:00", status=0)
        self.db.add_all([self.one, self.two, self.foreign])
        self.db.commit()
        self.ctx = AgentContext(db=self.db, user=self.user, conversation_id="cancel-session")

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def prepare(self, query, *, ctx=None, model_intent="none"):
        state = {"user_query": query, "today": "2099-10-11", "history": []}
        result = {"slots": {}, "missing_slots": [], "authorized": False,
                  "allowed_write_tools": [], "plan_needed": False, "write_followup": model_intent}
        return graph._prepare_cancel_draft(ctx or self.ctx, state, result)

    def save_from_reply(self):
        graph._save_cancel_candidates(self.ctx, {
            "user_query": "帮我取消10月13号的预约",
            "today": "2099-10-11",
            "slots": {"date": "2099-10-13"},
            "tool_results": [],
        }, f"预约号 {self.one.id} 和预约号 {self.two.id}，请选一条")

    def test_all_cancel_uses_exact_candidates(self):
        initial = self.prepare("帮我取消10月13号的预约")
        self.assertTrue(initial["cancel_pending"])
        self.assertEqual(initial["allowed_write_tools"], [])
        self.save_from_reply()
        result = self.prepare("都取消")
        self.assertEqual(result["cancel_target_ids"], [self.one.id, self.two.id])
        self.assertEqual(result["allowed_write_tools"], ["cancel_reservations"])
        outcome = drafts.cancel_selected(self.db, self.user.id, "cancel-session", result["cancel_revision"], result["cancel_target_ids"])
        self.assertTrue(outcome["ok"])
        self.db.refresh(self.one)
        self.db.refresh(self.two)
        self.db.refresh(self.foreign)
        self.assertEqual((self.one.status, self.two.status, self.foreign.status), (3, 3, 0))
        self.assertFalse(drafts.cancel_selected(self.db, self.user.id, "cancel-session", result["cancel_revision"], result["cancel_target_ids"])["ok"])

    def test_response_lists_database_candidates_and_saves_them(self):
        initial = self.prepare("帮我取消10月13号的预约")
        state = {
            "user_query": "帮我取消10月13号的预约",
            "today": "2099-10-11",
            "slots": {"date": "2099-10-13"},
            "tool_results": [],
            "messages": [],
            **initial,
        }

        @contextmanager
        def no_trace(*_args, **_kwargs):
            yield {"output": ""}

        with patch.object(graph, "trace_node", no_trace):
            answer = graph.node_respond(self.ctx)(state)["final_response"]
        self.assertIn(f"预约号 {self.one.id}", answer)
        self.assertIn(f"预约号 {self.two.id}", answer)
        self.assertEqual(
            drafts.load(self.db, self.user.id, "cancel-session").reservation_ids,
            [self.one.id, self.two.id],
        )

    def test_selected_one_and_wrong_target(self):
        self.save_from_reply()
        result = self.prepare(f"预约号{self.one.id}")
        self.assertEqual(result["cancel_target_ids"], [self.one.id])
        self.assertEqual(self.prepare("上午八点到十点")["cancel_target_ids"], [self.one.id])
        self.assertEqual(self.prepare("14:00–17:00")["cancel_target_ids"], [self.two.id])
        self.assertFalse(drafts.cancel_selected(self.db, self.user.id, "cancel-session", result["cancel_revision"], [self.foreign.id])["ok"])
        self.assertTrue(drafts.cancel_selected(self.db, self.user.id, "cancel-session", result["cancel_revision"], [self.one.id])["ok"])
        self.db.refresh(self.two)
        self.assertEqual(self.two.status, 0)

    def test_atomic_failure_when_one_changed(self):
        self.save_from_reply()
        self.two.status = 1
        self.db.commit()
        result = self.prepare("都取消")
        self.assertFalse(drafts.cancel_selected(self.db, self.user.id, "cancel-session", result["cancel_revision"], result["cancel_target_ids"])["ok"])
        self.db.refresh(self.one)
        self.assertEqual(self.one.status, 0)

    def test_expiry_user_conversation_and_noncommands(self):
        self.save_from_reply()
        self.assertEqual(self.prepare("都取消吗")["allowed_write_tools"], [])
        self.assertEqual(self.prepare("都取消，先别操作", model_intent="cancel_all")["allowed_write_tools"], [])
        self.assertEqual(self.prepare("那两条都不要了", model_intent="cancel_all")["cancel_target_ids"], [self.one.id, self.two.id])
        other_ctx = AgentContext(db=self.db, user=self.other, conversation_id="cancel-session")
        self.assertEqual(self.prepare("都取消", ctx=other_ctx)["allowed_write_tools"], [])
        other_session = AgentContext(db=self.db, user=self.user, conversation_id="other-session")
        self.assertEqual(self.prepare("都取消", ctx=other_session)["allowed_write_tools"], [])
        draft = drafts.load(self.db, self.user.id, "cancel-session")
        draft.expires_at = datetime.now() - timedelta(seconds=1)
        self.db.commit()
        self.assertEqual(self.prepare("都取消")["allowed_write_tools"], [])
        self.save_from_reply()
        self.assertEqual(self.prepare("不要取消")["allowed_write_tools"], [])
        self.assertIsNone(drafts.load(self.db, self.user.id, "cancel-session"))


if __name__ == "__main__":
    unittest.main()
