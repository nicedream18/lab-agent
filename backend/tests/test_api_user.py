"""用户接口（app/api/user.py + app/services/user_service.py）。

这里有一条必须钉死的安全边界：
  普通用户走 PUT /api/user/update 改自己资料时，**不能**顺手把 role 改成 admin、
  也不能把自己 status 改成 0 或 1 —— 那两个字段在 service 层被显式排除了。
管理员走 PUT /api/user/{user_id} 才能改 role/status。
两条路径的字段白名单不一样，是本文件最值得看的地方。
"""

from __future__ import annotations

import unittest

import app.models  # noqa: F401
from tests.api_harness import ApiTestCase

from app.models.user import User
from app.utils.password import verify_password

PASSWORD = "pw-123456"
INFO = "/api/user/info"
UPDATE_ME = "/api/user/update"
CHANGE_PASSWORD = "/api/user/password"
LIST = "/api/user/list"
CREATE = "/api/user"


class ProfileCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan", password=PASSWORD, name="张三")

    def test_info_returns_the_current_user(self) -> None:
        data = self.assert_success(self.client.get(INFO, headers=self.auth(self.student)))
        self.assertEqual(data["id"], self.student.id)
        self.assertEqual(data["username"], "zhangsan")
        self.assertEqual(data["name"], "张三")
        self.assertEqual(data["role"], "student")
        self.assertNotIn("password", data)

    def test_info_follows_the_token_not_any_query_parameter(self) -> None:
        """没有任何「取谁的信息」的入参 —— 只能是 token 里那个人。"""
        other = self.make_user("lisi", password=PASSWORD)
        data = self.assert_success(self.client.get(INFO, headers=self.auth(other)))
        self.assertEqual(data["id"], other.id)


class UpdateProfileCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan", password=PASSWORD, name="张三")

    def test_a_user_updates_their_own_profile(self) -> None:
        data = self.assert_success(
            self.client.put(
                UPDATE_ME,
                json={"name": "张三丰", "email": "z@example.com", "phone": "13800000000"},
                headers=self.auth(self.student),
            )
        )
        self.assertEqual(data["name"], "张三丰")
        self.assertEqual(data["email"], "z@example.com")
        self.assertEqual(data["phone"], "13800000000")

        self.reload(self.student)
        self.assertEqual(self.student.name, "张三丰")

    def test_omitted_fields_are_left_untouched(self) -> None:
        """exclude_none=True：没传的字段保持原值，而不是被写成 NULL。"""
        self.client.put(UPDATE_ME, json={"name": "只改名字"}, headers=self.auth(self.student))
        self.reload(self.student)
        self.assertEqual(self.student.name, "只改名字")
        self.assertEqual(self.student.email, None)

    def test_a_user_cannot_promote_themselves_to_admin(self) -> None:
        """越权提权的经典路径：请求体里塞 role=admin。"""
        data = self.assert_success(
            self.client.put(
                UPDATE_ME,
                json={"name": "张三", "role": "admin", "status": 0},
                headers=self.auth(self.student),
            )
        )
        self.assertEqual(data["role"], "student")
        self.assertEqual(data["status"], 1)

        self.reload(self.student)
        self.assertEqual(self.student.role, "student")
        self.assertEqual(self.student.status, 1)

    def test_update_me_is_not_swallowed_by_the_admin_route(self) -> None:
        """路由顺序陷阱：PUT /user/update 与 PUT /user/{user_id} 形状一样。

        如果 /user/{user_id} 先注册，/user/update 会被当成 user_id="update"
        直接抛 422。这里断言它精确命中 update_me 这条路由 ——
        学生改自己的资料成功，且**没有**真的去改 id=update 的用户。
        """
        response = self.client.put(
            UPDATE_ME, json={"name": "改自己"}, headers=self.auth(self.student)
        )
        self.assert_success(response)  # 不是 422
        self.reload(self.student)
        self.assertEqual(self.student.name, "改自己")

    def test_update_requires_authentication(self) -> None:
        self.assert_unauthorized(self.client.put(UPDATE_ME, json={"name": "谁"}))

    def test_validation_error_for_a_wrong_type(self) -> None:
        self.assert_validation_error(
            self.client.put(UPDATE_ME, json={"status": "不是数字"}, headers=self.auth(self.student))
        )


class ChangePasswordCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan", password=PASSWORD)

    def test_change_password_then_the_old_one_stops_working(self) -> None:
        response = self.client.put(
            CHANGE_PASSWORD,
            json={"old_password": PASSWORD, "new_password": "new-pw-987654"},
            headers=self.auth(self.student),
        )
        self.assert_success(response, message="密码修改成功")

        self.reload(self.student)
        self.assertTrue(verify_password("new-pw-987654", self.student.password))
        self.assertFalse(verify_password(PASSWORD, self.student.password))

        # 端到端确认：新密码能登录，旧密码不能
        self.assert_success(
            self.client.post(
                "/api/auth/login",
                json={"username": "zhangsan", "password": "new-pw-987654"},
            )
        )
        self.assert_business_error(
            self.client.post(
                "/api/auth/login", json={"username": "zhangsan", "password": PASSWORD}
            ),
            "账号或密码错误",
        )

    def test_overlong_new_password_is_a_validation_error(self) -> None:
        self.assert_validation_error(self.client.put(
            CHANGE_PASSWORD,
            json={"old_password": PASSWORD, "new_password": "a" * 73},
            headers=self.auth(self.student),
        ))
        self.reload(self.student)
        self.assertTrue(verify_password(PASSWORD, self.student.password))

    def test_wrong_old_password_is_rejected(self) -> None:
        response = self.client.put(
            CHANGE_PASSWORD,
            json={"old_password": "猜的", "new_password": "new-pw-987654"},
            headers=self.auth(self.student),
        )
        self.assert_business_error(response, "旧密码不正确")
        self.reload(self.student)
        self.assertTrue(verify_password(PASSWORD, self.student.password))

    def test_the_new_password_must_differ_from_the_old_one(self) -> None:
        response = self.client.put(
            CHANGE_PASSWORD,
            json={"old_password": PASSWORD, "new_password": PASSWORD},
            headers=self.auth(self.student),
        )
        self.assert_business_error(response, "新密码不能与旧密码相同")

    def test_cannot_change_someone_elses_password(self) -> None:
        """这个接口没有 user_id 入参，天然只能改自己。"""
        other = self.make_user("lisi", password=PASSWORD)
        self.client.put(
            CHANGE_PASSWORD,
            json={"old_password": PASSWORD, "new_password": "new-pw-987654"},
            headers=self.auth(self.student),
        )
        self.reload(other)
        self.assertTrue(verify_password(PASSWORD, other.password))


class AdminUserListCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.make_admin(password=PASSWORD)
        self.zhangsan = self.make_user("zhangsan", password=PASSWORD, name="张三")
        self.lisi = self.make_user("lisi", password=PASSWORD, name="李四")

    def test_a_student_cannot_list_users(self) -> None:
        response = self.client.get(LIST, headers=self.auth(self.zhangsan))
        self.assert_business_error(response, "无权限访问", code=403)

    def test_admin_gets_a_page_response(self) -> None:
        data = self.assert_success(self.client.get(LIST, headers=self.auth(self.admin)))
        self.assertIn("list", data)
        self.assertIn("total", data)
        self.assertEqual(data["total"], 3)  # 管理员自己 + 两个学生

    def test_ordering_is_newest_first(self) -> None:
        data = self.assert_success(self.client.get(LIST, headers=self.auth(self.admin)))
        ids = [item["id"] for item in data["list"]]
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_keyword_matches_username_or_real_name(self) -> None:
        by_username = self.assert_success(
            self.client.get(LIST + "?keywords=zhang", headers=self.auth(self.admin))
        )
        self.assertEqual([item["username"] for item in by_username["list"]], ["zhangsan"])

        by_name = self.assert_success(
            self.client.get(LIST + "?keywords=李四", headers=self.auth(self.admin))
        )
        self.assertEqual([item["username"] for item in by_name["list"]], ["lisi"])

    def test_keyword_with_no_match_returns_an_empty_page_not_an_error(self) -> None:
        data = self.assert_success(
            self.client.get(LIST + "?keywords=查无此人", headers=self.auth(self.admin))
        )
        self.assertEqual(data["list"], [])
        self.assertEqual(data["total"], 0)

    def test_paging_splits_the_total(self) -> None:
        first = self.assert_success(
            self.client.get(LIST + "?page=1&page_size=2", headers=self.auth(self.admin))
        )
        second = self.assert_success(
            self.client.get(LIST + "?page=2&page_size=2", headers=self.auth(self.admin))
        )
        self.assertEqual(first["total"], 3)
        self.assertEqual(second["total"], 3)  # total 是总数，不是本页条数
        self.assertEqual(len(first["list"]), 2)
        self.assertEqual(len(second["list"]), 1)
        overlap = {item["id"] for item in first["list"]} & {
            item["id"] for item in second["list"]
        }
        self.assertEqual(overlap, set())


class AdminUserWriteCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.make_admin(password=PASSWORD)
        self.zhangsan = self.make_user("zhangsan", password=PASSWORD, name="张三")

    def test_admin_creates_a_user_and_the_default_password_is_hashed(self) -> None:
        data = self.assert_success(
            self.client.post(
                CREATE,
                json={"username": "newbie", "name": "新人", "role": "teacher"},
                headers=self.auth(self.admin),
            )
        )
        self.assertEqual(data["username"], "newbie")
        self.assertEqual(data["role"], "teacher")

        created = self.db.query(User).filter_by(username="newbie").one()
        # UserCreateRequest.password 默认 "123"，但入库的必须是 hash
        self.assertNotEqual(created.password, "123")
        self.assertTrue(verify_password("123", created.password))

    def test_admin_cannot_create_a_user_with_an_overlong_password(self) -> None:
        self.assert_validation_error(self.client.post(
            CREATE,
            json={"username": "too-long", "password": "a" * 73},
            headers=self.auth(self.admin),
        ))
        self.assertEqual(self.db.query(User).filter_by(username="too-long").count(), 0)

    def test_creating_a_duplicate_username_is_rejected(self) -> None:
        response = self.client.post(
            CREATE,
            json={"username": "zhangsan", "password": PASSWORD},
            headers=self.auth(self.admin),
        )
        self.assert_business_error(response, "用户名已存在")

    def test_a_student_cannot_create_users(self) -> None:
        response = self.client.post(
            CREATE, json={"username": "hacker"}, headers=self.auth(self.zhangsan)
        )
        self.assert_business_error(response, "无权限访问", code=403)
        self.assertEqual(self.db.query(User).filter_by(username="hacker").count(), 0)

    def test_admin_can_change_role_and_status(self) -> None:
        """和自己改自己不同：这条路径**允许**改 role/status。"""
        data = self.assert_success(
            self.client.put(
                f"/api/user/{self.zhangsan.id}",
                json={"role": "teacher", "status": 0},
                headers=self.auth(self.admin),
            )
        )
        self.assertEqual(data["role"], "teacher")
        self.assertEqual(data["status"], 0)

        self.reload(self.zhangsan)
        self.assertEqual(self.zhangsan.role, "teacher")
        self.assertEqual(self.zhangsan.status, 0)
        # 状态被置 0 之后，这个人的 token 立刻失效
        self.assert_unauthorized(
            self.client.get(INFO, headers=self.auth(self.zhangsan)), "用户被禁用"
        )

    def test_updating_a_missing_user_says_so(self) -> None:
        response = self.client.put(
            "/api/user/99999", json={"name": "查无此人"}, headers=self.auth(self.admin)
        )
        self.assert_business_error(response, "用户不存在")

    def test_admin_deletes_a_user(self) -> None:
        target = self.make_user("tobedeleted", password=PASSWORD)
        response = self.client.delete(f"/api/user/{target.id}", headers=self.auth(self.admin))
        self.assert_success(response)
        self.assertEqual(self.db.query(User).filter_by(username="tobedeleted").count(), 0)

    def test_admin_cannot_delete_their_own_account(self) -> None:
        """防呆：删掉自己会立刻把整个后台锁死。"""
        response = self.client.delete(
            f"/api/user/{self.admin.id}", headers=self.auth(self.admin)
        )
        self.assert_business_error(response, "不能删除当前登录的账号")
        self.assertEqual(self.db.query(User).filter_by(id=self.admin.id).count(), 1)

    def test_deleting_a_missing_user_says_so(self) -> None:
        response = self.client.delete("/api/user/99999", headers=self.auth(self.admin))
        self.assert_business_error(response, "用户不存在")

    def test_a_student_cannot_delete_anyone(self) -> None:
        target = self.make_user("victim", password=PASSWORD)
        response = self.client.delete(
            f"/api/user/{target.id}", headers=self.auth(self.zhangsan)
        )
        self.assert_business_error(response, "无权限访问", code=403)
        self.assertEqual(self.db.query(User).filter_by(id=target.id).count(), 1)


if __name__ == "__main__":
    unittest.main()
