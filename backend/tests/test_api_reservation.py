"""预约接口（app/api/reservation.py + app/services/reservation_service.py）。

预约是全站规则最密的地方，也是唯一会「按当前时间」判定成败的接口，
所以这里的日期一律用 **相对日期** 算出来，绝不写死字符串 —— 写死的那天开始，
整套用例会在某个凌晨静默变成「过去时间」，然后红给你看。

另外两处值得注意的不对称（都已钉住）：
  - 列表接口用 `if status is not None`，所以 status=0 是**会**过滤的
    （对比 lab_service 里的 `if status:` —— 同一类筛选，两套口径）
  - 取消接口要求 status==0，也就是说**已通过（status=1）的预约学生自己取消不了**，
    只能等管理员处理
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import app.models  # noqa: F401
from app.models.reservation import Reservation
from app.services.reservation_service import MAX_ACTIVE_RESERVATIONS

from tests.api_harness import ApiTestCase

CREATE = "/api/reservation"
LIST = "/api/reservation/list"

# 实验室开放窗口（fixture 用）：08:00 - 20:00
SLOT_IN_WINDOW = ("09:00", "11:00")


def _day(offset: int) -> str:
    """相对今天偏移 offset 天的 ISO 日期。"""
    return (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")


def _now_hhmm() -> str:
    return datetime.now().strftime("%H:%M")


class CreateReservationCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")
        self.lab = self.make_lab("计算机实验室")
        self.tomorrow = _day(1)

    def _payload(self, **overrides):
        body = {
            "lab_id": self.lab.id,
            "date": self.tomorrow,
            "start_time": SLOT_IN_WINDOW[0],
            "end_time": SLOT_IN_WINDOW[1],
        }
        body.update(overrides)
        return body

    def test_a_reservation_is_created_pending_review(self) -> None:
        data = self.assert_success(
            self.client.post(
                CREATE,
                json=self._payload(remark="做实验"),
                headers=self.auth(self.student),
            )
        )
        self.assertIsNone(data)  # 只回一句「请求成功」，前端靠列表刷新

        row = self.db.query(Reservation).one()
        self.assertEqual(row.user_id, self.student.id)
        self.assertEqual(row.lab_id, self.lab.id)
        self.assertEqual(row.status, 0)
        self.assertEqual(row.remark, "做实验")
        self.assertIsNone(row.equipment_id)

    def test_a_past_date_is_rejected(self) -> None:
        response = self.client.post(
            CREATE, json=self._payload(date=_day(-1)), headers=self.auth(self.student)
        )
        self.assert_business_error(response, "预约日期不能小于当前的日期")
        self.assertEqual(self.db.query(Reservation).count(), 0)

    def test_end_time_must_be_after_start_time(self) -> None:
        for start, end in (("11:00", "11:00"), ("11:00", "09:00")):
            with self.subTest(start=start, end=end):
                response = self.client.post(
                    CREATE,
                    json=self._payload(start_time=start, end_time=end),
                    headers=self.auth(self.student),
                )
                self.assert_business_error(response, "预约的结束时间必须晚于开始时间")

    def test_today_with_a_start_time_already_passed_is_rejected(self) -> None:
        current = _now_hhmm()
        if current == "00:00":
            self.skipTest("刚好跑到 00:00，没有「已经过去的时刻」可用")
        response = self.client.post(
            CREATE,
            json=self._payload(date=_day(0), start_time="00:00", end_time="00:30"),
            headers=self.auth(self.student),
        )
        self.assert_business_error(response, "预约开始时间不能小于当前的时间")

    def test_a_missing_lab_is_reported(self) -> None:
        response = self.client.post(
            CREATE, json=self._payload(lab_id=99999), headers=self.auth(self.student)
        )
        self.assert_business_error(response, "实验室不存在")

    def test_a_closed_lab_cannot_be_reserved(self) -> None:
        closed = self.make_lab("装修中的实验室", status=0)
        response = self.client.post(
            CREATE,
            json=self._payload(lab_id=closed.id),
            headers=self.auth(self.student),
        )
        self.assert_business_error(response, "实验室已关闭")

    def test_cannot_book_before_the_lab_opens(self) -> None:
        response = self.client.post(
            CREATE,
            json=self._payload(start_time="07:00", end_time="08:30"),
            headers=self.auth(self.student),
        )
        self.assert_business_error(response, "预约时间不能早于实验室的开放时间")

    def test_cannot_book_after_the_lab_closes(self) -> None:
        response = self.client.post(
            CREATE,
            json=self._payload(start_time="19:30", end_time="21:00"),
            headers=self.auth(self.student),
        )
        self.assert_business_error(response, "预约时间不能晚于实验室的关闭时间")

    def test_a_lab_without_open_hours_skips_the_window_check(self) -> None:
        """open_time/close_time 为 NULL 时不做窗口校验（老数据就是这样）。"""
        anytime = self.make_lab("全天开放实验室", open_time=None, close_time=None)
        self.assert_success(
            self.client.post(
                CREATE,
                json=self._payload(
                    lab_id=anytime.id, start_time="00:30", end_time="23:30"
                ),
                headers=self.auth(self.student),
            )
        )

    def test_an_overlapping_slot_is_rejected(self) -> None:
        self.assert_success(
            self.client.post(
                CREATE, json=self._payload(), headers=self.auth(self.student)
            )
        )
        other = self.make_user("lisi")
        response = self.client.post(
            CREATE,
            json=self._payload(start_time="10:00", end_time="12:00"),
            headers=self.auth(other),
        )
        self.assert_business_error(response, "该时段已预约")

    def test_touching_endpoints_do_not_conflict(self) -> None:
        """[start, end) 左闭右开：上一场 11:00 结束，下一场 11:00 开始不算冲突。"""
        self.assert_success(
            self.client.post(
                CREATE, json=self._payload(), headers=self.auth(self.student)
            )
        )
        self.assert_success(
            self.client.post(
                CREATE,
                json=self._payload(start_time="11:00", end_time="13:00"),
                headers=self.auth(self.student),
            )
        )

    def test_a_cancelled_slot_frees_up(self) -> None:
        cancelled = self.make_reservation(
            self.student,
            self.lab,
            date=self.tomorrow,
            status=3,
            start_time=SLOT_IN_WINDOW[0],
            end_time=SLOT_IN_WINDOW[1],
        )
        self.assert_success(
            self.client.post(
                CREATE, json=self._payload(), headers=self.auth(self.student)
            )
        )
        self.reload(cancelled)
        self.assertEqual(cancelled.status, 3)

    def test_booking_an_equipment_does_not_collide_with_a_lab_booking(self) -> None:
        """设备预约与「整间实验室」预约是两条互不干扰的占用线。"""
        device = self.make_equipment(self.lab, "开发主机")
        self.assert_success(
            self.client.post(
                CREATE,
                json=self._payload(equipment_id=device.id),
                headers=self.auth(self.student),
            )
        )
        # 同一时段还可以约整间实验室
        self.assert_success(
            self.client.post(
                CREATE, json=self._payload(), headers=self.auth(self.student)
            )
        )
        types = [
            "设备" if row.equipment_id else "实验室"
            for row in self.db.query(Reservation).all()
        ]
        self.assertCountEqual(types, ["设备", "实验室"])

    def test_a_missing_device_is_reported(self) -> None:
        response = self.client.post(
            CREATE,
            json=self._payload(equipment_id=99999),
            headers=self.auth(self.student),
        )
        self.assert_business_error(response, "实验室设备不存在")

    def test_a_device_under_repair_cannot_be_reserved(self) -> None:
        broken = self.make_equipment(self.lab, "坏掉的主机", status=0)
        response = self.client.post(
            CREATE,
            json=self._payload(equipment_id=broken.id),
            headers=self.auth(self.student),
        )
        self.assert_business_error(response, "实验室设备正在维修")

    def test_the_active_quota_is_enforced(self) -> None:
        """一个人最多同时压着 5 条「待审核 + 已通过」的预约。"""
        for index in range(MAX_ACTIVE_RESERVATIONS):
            self.make_reservation(
                self.student,
                self.lab,
                date=_day(10 + index),
                start_time="09:00",
                end_time="10:00",
                status=0,
            )
        response = self.client.post(
            CREATE,
            json=self._payload(date=_day(10 + MAX_ACTIVE_RESERVATIONS)),
            headers=self.auth(self.student),
        )
        self.assert_business_error(
            response, f"进行中的预约已达上限 {MAX_ACTIVE_RESERVATIONS} 条"
        )

    def test_rejected_and_cancelled_rows_do_not_count_against_the_quota(self) -> None:
        for index in range(MAX_ACTIVE_RESERVATIONS):
            self.make_reservation(
                self.student, self.lab, date=_day(20 + index), status=3 if index else 2
            )
        self.assert_success(
            self.client.post(
                CREATE, json=self._payload(), headers=self.auth(self.student)
            )
        )

    def test_creation_requires_authentication(self) -> None:
        self.assert_unauthorized(self.client.post(CREATE, json=self._payload()))

    def test_lab_id_is_required(self) -> None:
        self.assert_validation_error(
            self.client.post(
                CREATE, json=self._payload(lab_id=None), headers=self.auth(self.student)
            )
        )


class ListReservationCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan", name="张三")
        self.other = self.make_user("lisi", name="李四")
        self.admin = self.make_admin(name="管理员")
        self.lab = self.make_lab("计算机实验室")
        self.tomorrow = _day(1)

        self.mine = self.make_reservation(
            self.student,
            self.lab,
            date=self.tomorrow,
            start_time="09:00",
            end_time="10:00",
        )
        self.theirs = self.make_reservation(
            self.other,
            self.lab,
            date=self.tomorrow,
            start_time="11:00",
            end_time="12:00",
            status=1,
        )

    def test_a_student_only_sees_their_own_reservations(self) -> None:
        data = self.assert_success(
            self.client.get(LIST, headers=self.auth(self.student))
        )
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["list"][0]["id"], self.mine.id)

    def test_an_admin_sees_every_reservation(self) -> None:
        data = self.assert_success(self.client.get(LIST, headers=self.auth(self.admin)))
        self.assertEqual(data["total"], 2)
        self.assertEqual({item["user_name"] for item in data["list"]}, {"张三", "李四"})

    def test_relations_and_type_are_filled_in(self) -> None:
        item = self.assert_success(
            self.client.get(LIST, headers=self.auth(self.student))
        )["list"][0]
        self.assertEqual(item["user_name"], "张三")
        self.assertEqual(item["lab_name"], "计算机实验室")
        self.assertIsNone(item["equipment_name"])
        self.assertEqual(item["type"], "实验室")

    def test_an_equipment_reservation_is_tagged_as_a_device(self) -> None:
        device = self.make_equipment(self.lab, "开发主机")
        self.client.post(
            CREATE,
            json={
                "lab_id": self.lab.id,
                "equipment_id": device.id,
                "date": _day(2),
                "start_time": "09:00",
                "end_time": "10:00",
            },
            headers=self.auth(self.student),
        )
        data = self.assert_success(
            self.client.get(LIST, headers=self.auth(self.student))
        )
        by_id = {item["id"]: item for item in data["list"]}
        device_item = [
            item for item in data["list"] if item["equipment_name"] == "开发主机"
        ][0]
        self.assertEqual(device_item["type"], "设备")
        self.assertEqual(len(by_id), 2)

    def test_status_zero_does_filter_here(self) -> None:
        """和 lab 的列表接口形成对照：那边 `if status:` 会漏掉 0，这边不会。"""
        pending = self.assert_success(
            self.client.get(LIST + "?status=0", headers=self.auth(self.student))
        )
        approved = self.assert_success(
            self.client.get(LIST + "?status=1", headers=self.auth(self.student))
        )
        self.assertEqual([item["id"] for item in pending["list"]], [self.mine.id])
        self.assertEqual(approved["list"], [])

        admin_approved = self.assert_success(
            self.client.get(LIST + "?status=1", headers=self.auth(self.admin))
        )
        self.assertEqual(
            [item["id"] for item in admin_approved["list"]], [self.theirs.id]
        )

    def test_ordering_is_newest_first(self) -> None:
        data = self.assert_success(self.client.get(LIST, headers=self.auth(self.admin)))
        ids = [item["id"] for item in data["list"]]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_paging(self) -> None:
        first = self.assert_success(
            self.client.get(LIST + "?page=1&page_size=1", headers=self.auth(self.admin))
        )
        second = self.assert_success(
            self.client.get(LIST + "?page=2&page_size=1", headers=self.auth(self.admin))
        )
        self.assertEqual(first["total"], 2)
        self.assertEqual(len(first["list"]), 1)
        self.assertEqual(len(second["list"]), 1)
        self.assertNotEqual(first["list"][0]["id"], second["list"][0]["id"])

    def test_listing_requires_authentication(self) -> None:
        self.assert_unauthorized(self.client.get(LIST))


class CancelReservationCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")
        self.other = self.make_user("lisi")
        self.admin = self.make_admin()
        self.lab = self.make_lab("计算机实验室")

    def test_a_student_cancels_their_pending_reservation(self) -> None:
        reservation = self.make_reservation(self.student, self.lab, status=0)
        self.assert_success(
            self.client.put(
                f"/api/reservation/{reservation.id}/cancel",
                headers=self.auth(self.student),
            )
        )
        self.reload(reservation)
        self.assertEqual(reservation.status, 3)

    def test_cancelling_removes_it_from_the_active_quota(self) -> None:
        reservation = self.make_reservation(self.student, self.lab, status=0)
        self.client.put(
            f"/api/reservation/{reservation.id}/cancel", headers=self.auth(self.student)
        )
        self.assert_success(
            self.client.post(
                CREATE,
                json={
                    "lab_id": self.lab.id,
                    "date": _day(1),
                    "start_time": "09:00",
                    "end_time": "10:00",
                },
                headers=self.auth(self.student),
            )
        )

    def test_cannot_cancel_someone_elses_reservation(self) -> None:
        reservation = self.make_reservation(self.other, self.lab, status=0)
        response = self.client.put(
            f"/api/reservation/{reservation.id}/cancel", headers=self.auth(self.student)
        )
        self.assert_business_error(response, "预约不存在、无权限或当前状态无法取消")
        self.reload(reservation)
        self.assertEqual(reservation.status, 0)

    def test_an_already_approved_reservation_cannot_be_cancelled_by_the_student(
        self,
    ) -> None:
        """⚠️ 当前设计（钉住行为）：只有 status==0 能被学生取消。

        已被管理员通过（status=1）的预约，学生自己撤不掉，只能请管理员处理。
        这条如果哪天改成「已通过也能取消」，测试会红，提醒你这是一次行为变更
        而不是 bug 修复。
        """
        approved = self.make_reservation(self.student, self.lab, status=1)
        response = self.client.put(
            f"/api/reservation/{approved.id}/cancel", headers=self.auth(self.student)
        )
        self.assert_business_error(response, "预约不存在、无权限或当前状态无法取消")
        self.reload(approved)
        self.assertEqual(approved.status, 1)

    def test_admin_can_cancel_an_approved_reservation(self) -> None:
        reservation = self.make_reservation(self.student, self.lab, status=1)
        response = self.client.put(
            f"/api/reservation/{reservation.id}/cancel", headers=self.auth(self.admin)
        )
        self.assert_success(response)
        self.reload(reservation)
        self.assertEqual(reservation.status, 3)

    def test_admin_cannot_cancel_rejected_or_already_cancelled_reservations(
        self,
    ) -> None:
        for status in (2, 3):
            with self.subTest(status=status):
                reservation = self.make_reservation(
                    self.student, self.lab, status=status
                )
                response = self.client.put(
                    f"/api/reservation/{reservation.id}/cancel",
                    headers=self.auth(self.admin),
                )
                self.assert_business_error(
                    response, "预约不存在、无权限或当前状态无法取消"
                )
                self.reload(reservation)
                self.assertEqual(reservation.status, status)

    def test_cancelling_a_missing_reservation_reports_the_same_message(self) -> None:
        response = self.client.put(
            "/api/reservation/99999/cancel", headers=self.auth(self.student)
        )
        self.assert_business_error(response, "预约不存在、无权限或当前状态无法取消")

    def test_cancelling_requires_authentication(self) -> None:
        reservation = self.make_reservation(self.student, self.lab, status=0)
        self.assert_unauthorized(
            self.client.put(f"/api/reservation/{reservation.id}/cancel")
        )


class AuditReservationCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")
        self.admin = self.make_admin()
        self.lab = self.make_lab("计算机实验室")

    def _audit(self, reservation_id: int, status: int, user=None):
        return self.client.put(
            f"/api/reservation/{reservation_id}/audit",
            json={"status": status},
            headers=self.auth(user or self.admin),
        )

    def test_admin_approves_a_reservation(self) -> None:
        reservation = self.make_reservation(self.student, self.lab, status=0)
        self.assert_success(self._audit(reservation.id, 1))
        self.reload(reservation)
        self.assertEqual(reservation.status, 1)

    def test_admin_rejects_a_reservation(self) -> None:
        reservation = self.make_reservation(self.student, self.lab, status=0)
        self.assert_success(self._audit(reservation.id, 2))
        self.reload(reservation)
        self.assertEqual(reservation.status, 2)

    def test_a_rejected_reservation_frees_the_slot(self) -> None:
        reservation = self.make_reservation(
            self.student,
            self.lab,
            date=_day(1),
            start_time="09:00",
            end_time="10:00",
            status=0,
        )
        self._audit(reservation.id, 2)
        self.assert_success(
            self.client.post(
                CREATE,
                json={
                    "lab_id": self.lab.id,
                    "date": _day(1),
                    "start_time": "09:00",
                    "end_time": "10:00",
                },
                headers=self.auth(self.student),
            )
        )

    def test_only_one_and_two_are_valid_audit_states(self) -> None:
        reservation = self.make_reservation(self.student, self.lab, status=0)
        for status in (0, 3, 9):
            with self.subTest(status=status):
                self.assert_business_error(
                    self._audit(reservation.id, status), "审核状态错误"
                )
        self.reload(reservation)
        self.assertEqual(reservation.status, 0)

    def test_a_missing_reservation_is_reported(self) -> None:
        self.assert_business_error(self._audit(99999, 1), "预约记录不存在")

    def test_a_reservation_can_only_be_audited_once(self) -> None:
        reservation = self.make_reservation(self.student, self.lab, status=0)
        self._audit(reservation.id, 1)
        self.assert_business_error(self._audit(reservation.id, 2), "当前状态不支持审核")
        self.reload(reservation)
        self.assertEqual(reservation.status, 1)

    def test_a_cancelled_reservation_cannot_be_audited(self) -> None:
        reservation = self.make_reservation(self.student, self.lab, status=3)
        self.assert_business_error(self._audit(reservation.id, 1), "当前状态不支持审核")

    def test_a_student_cannot_audit(self) -> None:
        reservation = self.make_reservation(self.student, self.lab, status=0)
        response = self._audit(reservation.id, 1, user=self.student)
        self.assert_business_error(response, "无权限访问", code=403)
        self.reload(reservation)
        self.assertEqual(reservation.status, 0)

    def test_auditing_requires_a_valid_status_field(self) -> None:
        reservation = self.make_reservation(self.student, self.lab, status=0)
        response = self.client.put(
            f"/api/reservation/{reservation.id}/audit",
            json={},
            headers=self.auth(self.admin),
        )
        self.assert_validation_error(response)


if __name__ == "__main__":
    unittest.main()
