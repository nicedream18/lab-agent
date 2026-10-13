"""登录 / 注册接口（app/api/auth.py + app/services/auth.py）。

这两个接口是唯一不带鉴权的写接口，边界条件最多：
不存在的账号、密码错、账号被禁用、用户名重复 —— 都要有确定的响应，
不能出现「账号不存在」和「密码错误」两种不同提示（那等于送人一个账号枚举器）。
"""

from __future__ import annotations

import unittest

import app.models  # noqa: F401
from app.models.user import User
from app.utils.password import hash_password, verify_password

from tests.api_harness import ApiTestCase

LOGIN = "/api/auth/login"
REGISTER = "/api/auth/register"
PASSWORD = "pw-123456"


class LoginCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan", password=PASSWORD, name="张三")

    def test_login_returns_a_token_and_the_user_profile(self) -> None:
        data = self.assert_success(
            self.client.post(
                LOGIN, json={"username": "zhangsan", "password": PASSWORD}
            ),
            message="登录成功",
        )
        self.assertTrue(data["token"])
        self.assertEqual(data["user"]["username"], "zhangsan")
        self.assertEqual(data["user"]["name"], "张三")
        self.assertEqual(data["user"]["role"], "student")
        self.assertEqual(data["user"]["status"], 1)

        # 签出来的 token 必须真的能过鉴权，而不是只有个字符串
        info = self.client.get(
            "/api/user/info", headers={"Authorization": f"Bearer {data['token']}"}
        )
        self.assertEqual(self.assert_success(info)["id"], self.student.id)

    def test_login_response_never_contains_the_password_hash(self) -> None:
        data = self.assert_success(
            self.client.post(LOGIN, json={"username": "zhangsan", "password": PASSWORD})
        )
        self.assertNotIn("password", data["user"])

    def test_overlong_password_is_a_validation_error(self) -> None:
        self.assert_validation_error(
            self.client.post(LOGIN, json={"username": "zhangsan", "password": "a" * 73})
        )

    def test_password_limit_counts_utf8_bytes(self) -> None:
        self.assert_validation_error(
            self.client.post(
                LOGIN, json={"username": "zhangsan", "password": "密" * 25}
            )
        )

    def test_wrong_password_and_unknown_username_share_one_message(self) -> None:
        """两种失败必须一模一样，否则接口就成了账号枚举器。"""
        wrong_password = self.client.post(
            LOGIN, json={"username": "zhangsan", "password": "not-the-password"}
        )
        unknown_user = self.client.post(
            LOGIN, json={"username": "nobody", "password": PASSWORD}
        )
        self.assert_business_error(wrong_password, "账号或密码错误")
        self.assert_business_error(unknown_user, "账号或密码错误")
        self.assertEqual(wrong_password.json(), unknown_user.json())

    def test_disabled_account_cannot_log_in(self) -> None:
        self.make_user("banned", password=PASSWORD, status=0)
        response = self.client.post(
            LOGIN, json={"username": "banned", "password": PASSWORD}
        )
        self.assert_business_error(response, "账号被禁用")

    def test_username_is_case_sensitive(self) -> None:
        response = self.client.post(
            LOGIN, json={"username": "ZhangSan", "password": PASSWORD}
        )
        self.assert_business_error(response, "账号或密码错误")

    def test_missing_fields_are_rejected_before_hitting_the_service(self) -> None:
        self.assert_validation_error(self.client.post(LOGIN, json={}))
        self.assert_validation_error(
            self.client.post(LOGIN, json={"username": "zhangsan"})
        )

    def test_an_admin_logs_in_and_can_reach_admin_routes(self) -> None:
        admin = self.make_admin(password=PASSWORD)
        data = self.assert_success(
            self.client.post(LOGIN, json={"username": "admin", "password": PASSWORD})
        )
        self.assertEqual(data["user"]["role"], "admin")
        headers = {"Authorization": f"Bearer {data['token']}"}
        # 同一个 token：管理员能进，普通学生进不去
        self.assert_success(self.client.get("/api/user/list", headers=headers))
        self.assert_business_error(
            self.client.get("/api/user/list", headers=self.auth(self.student)),
            "无权限访问",
            code=403,
        )
        self.assertEqual(admin.role, "admin")


class RegisterCases(ApiTestCase):
    def test_register_succeeds_without_returning_any_data(self) -> None:
        data = self.assert_success(
            self.client.post(REGISTER, json={"username": "lisi", "password": PASSWORD}),
            message="注册成功",
        )
        self.assertIsNone(data)

    def test_registered_account_is_a_normal_student_with_a_hashed_password(
        self,
    ) -> None:
        self.client.post(
            REGISTER,
            json={"username": "lisi", "password": PASSWORD, "name": "李四"},
        )
        created = self.db.query(User).filter_by(username="lisi").one()
        self.assertEqual(created.role, "student")  # 注册永远拿不到管理员
        self.assertEqual(created.status, 1)
        self.assertEqual(created.name, "李四")
        # 明文绝不能入库
        self.assertNotEqual(created.password, PASSWORD)
        self.assertTrue(verify_password(PASSWORD, created.password))

    def test_name_falls_back_to_the_username(self) -> None:
        self.client.post(REGISTER, json={"username": "wangwu", "password": PASSWORD})
        created = self.db.query(User).filter_by(username="wangwu").one()
        self.assertEqual(created.name, "wangwu")

    def test_duplicate_username_is_rejected(self) -> None:
        self.make_user("zhangsan", password=PASSWORD)
        response = self.client.post(
            REGISTER, json={"username": "zhangsan", "password": PASSWORD}
        )
        self.assert_business_error(response, "账号已存在")

    def test_register_requires_a_password(self) -> None:
        """注意：这和 UserCreateRequest 不一样。

        UserCreateRequest（管理员建号）的 password 默认是 "123"，
        RegisterRequest（自助注册）是必填 —— 这条用例钉住这个差别，
        免得哪天有人「顺手统一」成默认密码。
        """
        self.assert_validation_error(
            self.client.post(REGISTER, json={"username": "no-password"})
        )
        self.assertEqual(
            self.db.query(User).filter_by(username="no-password").count(), 0
        )

    def test_overlong_password_is_rejected_without_creating_an_account(self) -> None:
        self.assert_validation_error(
            self.client.post(
                REGISTER, json={"username": "long-password", "password": "密" * 25}
            )
        )
        self.assertEqual(
            self.db.query(User).filter_by(username="long-password").count(), 0
        )

    def test_then_that_account_can_log_in(self) -> None:
        self.assert_success(
            self.client.post(
                REGISTER,
                json={"username": "zhaoliu", "password": PASSWORD, "name": "赵六"},
            )
        )
        data = self.assert_success(
            self.client.post(LOGIN, json={"username": "zhaoliu", "password": PASSWORD}),
            message="登录成功",
        )
        self.assertEqual(data["user"]["name"], "赵六")

    def test_login_matches_the_password_that_was_really_stored(self) -> None:
        """确认注册写进去的 hash 与登录时的 verify 是同一套算法。"""
        self.make_user("hash-check", password=PASSWORD)
        stored = self.db.query(User).filter_by(username="hash-check").one().password
        self.assertTrue(stored.startswith("$2"))
        self.assertNotEqual(stored, hash_password(PASSWORD))  # bcrypt 加盐，每次不同
        self.assertTrue(verify_password(PASSWORD, stored))


if __name__ == "__main__":
    unittest.main()
