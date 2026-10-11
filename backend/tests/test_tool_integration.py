"""真实 SQLAlchemy 服务的离线集成测试，使用独立内存 SQLite。"""

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from evals.offline_env import configure_offline

configure_offline()

# 注册所有表到 Base.metadata。
import app.models  # noqa: F401
from app.agent import tools
from app.agent.state import AgentContext
from app.database import Base
from app.models.lab import Lab
from app.models.reservation import Reservation
from app.models.user import User


class ReservationToolCases(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = User(
            username="student", password="hash", name="学生", role="student"
        )
        self.other = User(
            username="other", password="hash", name="其他人", role="student"
        )
        self.lab = Lab(
            name="计算机实验室", open_time="08:00", close_time="20:00", status=1
        )
        self.db.add_all([self.user, self.other, self.lab])
        self.db.commit()
        self.ctx = AgentContext(db=self.db, user=self.user, conversation_id="test")

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def reservation(self, owner=None, day="2099-01-01"):
        item = Reservation(
            user_id=(owner or self.user).id,
            lab_id=self.lab.id,
            date=day,
            start_time="14:00",
            end_time="16:00",
            status=0,
        )
        self.db.add(item)
        self.db.commit()
        return item

    def test_cannot_cancel_another_users_reservation(self):
        other_reservation = self.reservation(self.other)
        result = tools.run_tool(
            self.ctx, "cancel_reservation", {"reservation_id": other_reservation.id}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(self.db.get(Reservation, other_reservation.id).status, 0)

    def test_ambiguous_cancel_keeps_both_records(self):
        first = self.reservation()
        second = self.reservation()
        result = tools.run_tool(self.ctx, "cancel_reservation", {"date": "2099-01-01"})
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(
            [self.db.get(Reservation, key).status for key in (first.id, second.id)],
            [0, 0],
        )

    def test_create_and_verify_real_record(self):
        result = tools.run_tool(
            self.ctx,
            "create_reservation",
            {
                "lab_name": "计算机实验室",
                "date": "2099-01-02",
                "start_time": "14:00",
                "end_time": "16:00",
            },
        )
        self.assertTrue(result["ok"], result)
        row = self.db.get(Reservation, result["reservation_id"])
        self.assertEqual(
            (row.user_id, row.status, row.date), (self.user.id, 0, "2099-01-02")
        )


if __name__ == "__main__":
    unittest.main()
