"""实验室与设备接口回归：状态过滤、同实验室重名和响应字段。"""

from __future__ import annotations

import unittest

import app.models  # noqa: F401
from tests.api_harness import ApiTestCase

from app.models.equipment import Equipment
from app.models.lab import Lab

LAB_LIST = "/api/lab/list"
LAB_CREATE = "/api/lab"
EQUIPMENT_LIST = "/api/equipment/list"
EQUIPMENT_CREATE = "/api/equipment"


class LabReadCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")
        self.admin = self.make_admin()
        self.open_lab = self.make_lab("计算机实验室", status=1)
        self.closed_lab = self.make_lab("化学实验室", status=0)

    def test_any_logged_in_user_can_list_labs(self) -> None:
        data = self.assert_success(self.client.get(LAB_LIST, headers=self.auth(self.student)))
        self.assertEqual(data["total"], 2)
        self.assertIn("list", data)

    def test_listing_labs_requires_authentication(self) -> None:
        self.assert_unauthorized(self.client.get(LAB_LIST))

    def test_list_is_newest_first(self) -> None:
        data = self.assert_success(self.client.get(LAB_LIST, headers=self.auth(self.student)))
        ids = [item["id"] for item in data["list"]]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_keyword_filters_by_name(self) -> None:
        data = self.assert_success(
            self.client.get(LAB_LIST + "?keywords=化学", headers=self.auth(self.student))
        )
        self.assertEqual([item["name"] for item in data["list"]], ["化学实验室"])

    def test_status_one_filters_to_open_labs(self) -> None:
        data = self.assert_success(
            self.client.get(LAB_LIST + "?status=1", headers=self.auth(self.student))
        )
        self.assertEqual([item["name"] for item in data["list"]], ["计算机实验室"])

    def test_status_zero_filters_closed_labs(self) -> None:
        data = self.assert_success(
            self.client.get(LAB_LIST + "?status=0", headers=self.auth(self.student))
        )
        self.assertEqual(data["total"], 1)
        names = {item["name"] for item in data["list"]}
        self.assertEqual(names, {"化学实验室"})

    def test_get_one_lab_by_id(self) -> None:
        data = self.assert_success(
            self.client.get(f"/api/lab/{self.open_lab.id}", headers=self.auth(self.student))
        )
        self.assertEqual(data["name"], "计算机实验室")
        self.assertEqual(data["capacity"], 30)
        self.assertEqual(data["open_time"], "08:00")

    def test_getting_a_missing_lab_reports_it(self) -> None:
        response = self.client.get("/api/lab/99999", headers=self.auth(self.student))
        self.assert_business_error(response, "实验室不存在")

    def test_lab_id_must_be_an_integer(self) -> None:
        self.assert_validation_error(self.client.get("/api/lab/abc", headers=self.auth(self.student)))


class LabWriteCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")
        self.admin = self.make_admin()

    def test_admin_creates_a_lab(self) -> None:
        data = self.assert_success(
            self.client.post(
                LAB_CREATE,
                json={
                    "name": "物理实验室",
                    "location": "B座202",
                    "capacity": 24,
                    "open_time": "09:00",
                    "close_time": "18:00",
                },
                headers=self.auth(self.admin),
            )
        )
        self.assertEqual(data["name"], "物理实验室")
        self.assertEqual(data["status"], 1)  # 默认开放
        self.assertEqual(self.db.query(Lab).filter_by(name="物理实验室").count(), 1)

    def test_a_student_cannot_create_a_lab(self) -> None:
        response = self.client.post(
            LAB_CREATE, json={"name": "偷偷建的"}, headers=self.auth(self.student)
        )
        self.assert_business_error(response, "无权限访问", code=403)
        self.assertEqual(self.db.query(Lab).filter_by(name="偷偷建的").count(), 0)

    def test_duplicate_lab_name_is_rejected(self) -> None:
        self.make_lab("计算机实验室")
        response = self.client.post(
            LAB_CREATE, json={"name": "计算机实验室"}, headers=self.auth(self.admin)
        )
        self.assert_business_error(response, "实验室名称已存在")

    def test_name_is_required(self) -> None:
        self.assert_validation_error(
            self.client.post(LAB_CREATE, json={"capacity": 10}, headers=self.auth(self.admin))
        )

    def test_admin_updates_a_lab(self) -> None:
        lab = self.make_lab("计算机实验室")
        data = self.assert_success(
            self.client.put(
                f"/api/lab/{lab.id}",
                json={"capacity": 50, "status": 0, "location": "C座"},
                headers=self.auth(self.admin),
            )
        )
        self.assertEqual(data["capacity"], 50)
        self.assertEqual(data["status"], 0)

        self.reload(lab)
        self.assertEqual(lab.location, "C座")
        self.assertEqual(lab.name, "计算机实验室")  # 没传的字段保持原值

    def test_renaming_to_an_existing_name_is_rejected(self) -> None:
        self.make_lab("计算机实验室")
        other = self.make_lab("化学实验室")
        response = self.client.put(
            f"/api/lab/{other.id}",
            json={"name": "计算机实验室"},
            headers=self.auth(self.admin),
        )
        self.assert_business_error(response, "实验室名称已存在")

    def test_keeping_your_own_name_is_allowed(self) -> None:
        """查重时必须排除自己，否则「只改容量」会因为撞上自己而失败。"""
        lab = self.make_lab("计算机实验室")
        data = self.assert_success(
            self.client.put(
                f"/api/lab/{lab.id}",
                json={"name": "计算机实验室", "capacity": 66},
                headers=self.auth(self.admin),
            )
        )
        self.assertEqual(data["capacity"], 66)

    def test_updating_a_missing_lab_reports_it(self) -> None:
        response = self.client.put(
            "/api/lab/99999", json={"capacity": 1}, headers=self.auth(self.admin)
        )
        self.assert_business_error(response, "实验室不存在")

    def test_admin_deletes_a_lab(self) -> None:
        lab = self.make_lab("临时实验室")
        response = self.client.delete(f"/api/lab/{lab.id}", headers=self.auth(self.admin))
        self.assert_success(response, message="删除成功")
        self.assertEqual(self.db.query(Lab).filter_by(id=lab.id).count(), 0)

    def test_deleting_a_missing_lab_reports_it(self) -> None:
        response = self.client.delete("/api/lab/99999", headers=self.auth(self.admin))
        self.assert_business_error(response, "实验室不存在")

    def test_a_student_cannot_delete_a_lab(self) -> None:
        lab = self.make_lab("计算机实验室")
        response = self.client.delete(f"/api/lab/{lab.id}", headers=self.auth(self.student))
        self.assert_business_error(response, "无权限访问", code=403)
        self.assertEqual(self.db.query(Lab).filter_by(id=lab.id).count(), 1)


class EquipmentReadCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")
        self.lab_a = self.make_lab("计算机实验室")
        self.lab_b = self.make_lab("化学实验室")
        self.host = self.make_equipment(self.lab_a, "开发主机")
        self.microscope = self.make_equipment(self.lab_b, "光学显微镜")

    def test_listing_devices_fills_in_the_lab_name(self) -> None:
        data = self.assert_success(
            self.client.get(EQUIPMENT_LIST, headers=self.auth(self.student))
        )
        self.assertEqual(data["total"], 2)
        names = {item["name"]: item["lab_name"] for item in data["list"]}
        self.assertEqual(names["开发主机"], "计算机实验室")
        self.assertEqual(names["光学显微镜"], "化学实验室")

    def test_filtering_by_lab(self) -> None:
        data = self.assert_success(
            self.client.get(
                f"{EQUIPMENT_LIST}?lab_id={self.lab_a.id}", headers=self.auth(self.student)
            )
        )
        self.assertEqual([item["name"] for item in data["list"]], ["开发主机"])

    def test_keyword_filters_by_device_name(self) -> None:
        data = self.assert_success(
            self.client.get(EQUIPMENT_LIST + "?keywords=显微镜", headers=self.auth(self.student))
        )
        self.assertEqual([item["name"] for item in data["list"]], ["光学显微镜"])

    def test_listing_devices_requires_authentication(self) -> None:
        self.assert_unauthorized(self.client.get(EQUIPMENT_LIST))

    def test_a_lab_that_is_not_closed_hides_nothing_here(self) -> None:
        """清单接口不做实验室状态过滤 —— 关闭实验室的设备照样能被查到。

        这本身没错（工具要先能看到设备才谈得上预约），钉住它免得被误当漏洞改掉。
        """
        self.lab_b.status = 0
        self.db.commit()
        data = self.assert_success(
            self.client.get(EQUIPMENT_LIST, headers=self.auth(self.student))
        )
        self.assertEqual(data["total"], 2)


class EquipmentWriteCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")
        self.admin = self.make_admin()
        self.lab_a = self.make_lab("计算机实验室")
        self.lab_b = self.make_lab("化学实验室")

    def test_admin_creates_a_device(self) -> None:
        data = self.assert_success(
            self.client.post(
                EQUIPMENT_CREATE,
                json={"lab_id": self.lab_a.id, "name": "开发主机", "spec": "i7/32G", "quantity": 5},
                headers=self.auth(self.admin),
            )
        )
        self.assertEqual(data["name"], "开发主机")
        self.assertEqual(data["lab_id"], self.lab_a.id)
        self.assertEqual(data["lab_name"], "计算机实验室")  # 创建时会回填
        self.assertEqual(data["quantity"], 5)

    def test_a_student_cannot_create_a_device(self) -> None:
        response = self.client.post(
            EQUIPMENT_CREATE,
            json={"lab_id": self.lab_a.id, "name": "偷偷加的"},
            headers=self.auth(self.student),
        )
        self.assert_business_error(response, "无权限访问", code=403)

    def test_new_device_needs_a_lab_id(self) -> None:
        self.assert_validation_error(
            self.client.post(
                EQUIPMENT_CREATE, json={"name": "没有实验室"}, headers=self.auth(self.admin)
            )
        )

    def test_duplicate_device_name_across_labs_is_allowed(self) -> None:
        self.make_equipment(self.lab_a, "打印机")
        response = self.client.post(
            EQUIPMENT_CREATE,
            json={"lab_id": self.lab_b.id, "name": "打印机"},
            headers=self.auth(self.admin),
        )
        self.assertEqual(self.assert_success(response)["lab_id"], self.lab_b.id)

    def test_admin_updates_a_device(self) -> None:
        device = self.make_equipment(self.lab_a, "开发主机")
        response = self.client.put(
            f"/api/equipment/{device.id}",
            json={"quantity": 9, "status": 0},
            headers=self.auth(self.admin),
        )
        data = self.assert_success(response)
        self.assertEqual(data["quantity"], 9)
        self.assertEqual(data["status"], 0)

        self.reload(device)
        self.assertEqual(device.quantity, 9)

    def test_update_response_includes_the_lab_name(self) -> None:
        device = self.make_equipment(self.lab_a, "开发主机")
        data = self.assert_success(
            self.client.put(f"/api/equipment/{device.id}", json={"quantity": 2}, headers=self.auth(self.admin))
        )
        self.assertEqual(data["lab_name"], "计算机实验室")

        # 对照：列表接口里同一个设备的 lab_name 是有值的
        listed = self.assert_success(
            self.client.get(f"{EQUIPMENT_LIST}?lab_id={self.lab_a.id}", headers=self.auth(self.admin))
        )
        self.assertEqual(listed["list"][0]["lab_name"], "计算机实验室")

    def test_rename_conflicts_are_checked_within_the_same_lab_only(self) -> None:
        """与新增相反：更新时的同名查重只看**同一个实验室**。"""
        self.make_equipment(self.lab_a, "打印机")
        device = self.make_equipment(self.lab_b, "打印机")

        # 换成「显微镜」不冲突
        self.assert_success(
            self.client.put(
                f"/api/equipment/{device.id}", json={"name": "显微镜"}, headers=self.auth(self.admin)
            )
        )
        # 但换成 lab_a 里已经有的「打印机」就冲突了
        response = self.client.put(
            f"/api/equipment/{device.id}",
            json={"name": "打印机", "lab_id": self.lab_a.id},
            headers=self.auth(self.admin),
        )
        self.assert_business_error(response, "同一个实验室不能存在同名的设备")

    def test_moving_a_device_to_a_missing_lab_reports_it(self) -> None:
        device = self.make_equipment(self.lab_a, "开发主机")
        response = self.client.put(
            f"/api/equipment/{device.id}", json={"lab_id": 99999}, headers=self.auth(self.admin)
        )
        self.assert_business_error(response, "实验室不存在")

    def test_updating_a_missing_device_reports_it(self) -> None:
        response = self.client.put(
            "/api/equipment/99999", json={"quantity": 2}, headers=self.auth(self.admin)
        )
        self.assert_business_error(response, "实验室设备不存在")

    def test_admin_deletes_a_device(self) -> None:
        device = self.make_equipment(self.lab_a, "开发主机")
        self.assert_success(
            self.client.delete(f"/api/equipment/{device.id}", headers=self.auth(self.admin))
        )
        self.assertEqual(self.db.query(Equipment).filter_by(id=device.id).count(), 0)

    def test_deleting_a_missing_device_reports_it(self) -> None:
        response = self.client.delete("/api/equipment/99999", headers=self.auth(self.admin))
        self.assert_business_error(response, "实验室设备不存在")


if __name__ == "__main__":
    unittest.main()
