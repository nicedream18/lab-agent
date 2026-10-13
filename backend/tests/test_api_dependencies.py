"""鉴权依赖与加解密工具的单元测试。

这一层不跑 HTTP，而是直接把 `app.dependencies.auth` / `app.utils.jwt` /
`app.utils.password` 当普通函数调 —— 因为 HTTP 层的 401 已经在
`tests/test_api_contract.py` 里钉过了，这里补的是**函数内部的每个分支**：
token 坏在哪个环节，报出来的 detail 必须是不一样的，否则前端没法区分
「重新登录」和「账号有问题」。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from evals.offline_env import configure_offline

configure_offline()

import jwt  # noqa: E402
from app.config import settings  # noqa: E402
from app.dependencies.auth import get_current_admin, get_current_user  # noqa: E402
from app.models.user import User  # noqa: E402
from app.utils.jwt import create_access_token, decode_access_token  # noqa: E402
from app.utils.password import hash_password, verify_password  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from tests.api_harness import ApiTestCase  # noqa: E402


def _forge(payload: dict, secret: str | None = None) -> str:
    """按真实算法手搓一个 token，用来构造各种畸形 payload。"""
    return jwt.encode(
        payload,
        secret or settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


class GetCurrentUserCases(ApiTestCase):
    """get_current_user 的每一条拒绝分支。"""

    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")

    def _call(self, token: str):
        return get_current_user(token=token, db=self.db)

    def test_a_valid_token_returns_the_orm_user(self) -> None:
        user = self._call(create_access_token(self.student.id))
        self.assertIsInstance(user, User)
        self.assertEqual(user.id, self.student.id)

    def test_a_malformed_token_is_reported_as_an_expired_login(self) -> None:
        for token in ("not-a-jwt", "", "a.b.c"):
            with self.subTest(token=token):
                with self.assertRaises(HTTPException) as ctx:
                    self._call(token)
                self.assertEqual(ctx.exception.status_code, 401)
                self.assertEqual(ctx.exception.detail, "登录已失效，请重新登录")

    def test_a_token_signed_with_another_secret_is_rejected(self) -> None:
        token = _forge({"user_id": self.student.id}, secret="some-other-secret")
        with self.assertRaises(HTTPException) as ctx:
            self._call(token)
        self.assertEqual(ctx.exception.detail, "登录已失效，请重新登录")

    def test_an_expired_token_is_rejected(self) -> None:
        # 必须用带时区的 datetime：naive 的「一小时前」会被 PyJWT 当成 UTC，
        # 东八区算下来反而落在未来（详见下面 exp 漂移那条用例）
        token = _forge(
            {
                "user_id": self.student.id,
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            }
        )
        with self.assertRaises(HTTPException) as ctx:
            self._call(token)
        self.assertEqual(ctx.exception.detail, "登录已失效，请重新登录")

    def test_a_token_without_user_id_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call(_forge({"sub": "somebody"}))
        self.assertEqual(ctx.exception.detail, "无效的登录凭证")

    def test_a_user_id_of_zero_is_accepted_when_the_account_exists(self) -> None:
        zero = self.make_user("zero")
        zero.id = 0
        self.db.commit()
        self.assertEqual(self._call(_forge({"user_id": 0})).id, 0)

    def test_a_token_for_a_deleted_account_is_rejected(self) -> None:
        token = create_access_token(self.student.id)
        self.db.delete(self.student)
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            self._call(token)
        # 必须兜住 None，否则 user.status 会抛 AttributeError、用户拿到 500
        self.assertEqual(ctx.exception.detail, "无效的登录凭证")

    def test_a_disabled_account_is_rejected_with_its_own_message(self) -> None:
        token = create_access_token(self.student.id)
        self.student.status = 0
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            self._call(token)
        self.assertEqual(ctx.exception.detail, "用户被禁用")


class GetCurrentAdminCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")

    def test_an_admin_passes(self) -> None:
        admin = self.make_admin()
        self.assertEqual(get_current_admin(current_user=admin).id, admin.id)

    def test_a_student_is_rejected_with_403(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            get_current_admin(current_user=self.student)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "无权限访问")

    def test_the_role_check_is_case_sensitive(self) -> None:
        user = self.student
        user.role = "Admin"
        self.db.commit()
        with self.assertRaises(HTTPException):
            get_current_admin(current_user=user)


class JwtUtilsCases(unittest.TestCase):
    def test_create_and_decode_round_trip(self) -> None:
        token = create_access_token(7)
        self.assertIsInstance(token, str)
        self.assertEqual(len(token.split(".")), 3)
        self.assertEqual(decode_access_token(token)["user_id"], 7)

    def test_the_expiry_never_comes_out_shorter_than_configured(self) -> None:
        payload = decode_access_token(create_access_token(1))
        expire = datetime.fromtimestamp(payload["exp"])
        expected = datetime.now() + timedelta(hours=settings.JWT_EXPIRE_HOURS)
        self.assertGreaterEqual(expire - expected, timedelta(seconds=-60))

    def test_the_expiry_uses_utc_in_any_local_timezone(self) -> None:
        payload = decode_access_token(create_access_token(1))
        drift = datetime.fromtimestamp(payload["exp"], timezone.utc) - (
            datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRE_HOURS)
        )
        self.assertLess(abs(drift), timedelta(seconds=60))

    def test_decoding_with_a_tampered_signature_fails(self) -> None:
        token = create_access_token(1)
        head, body, signature = token.split(".")
        with self.assertRaises(jwt.InvalidSignatureError):
            decode_access_token(f"{head}.{body}.{signature[:-2]}xx")

    def test_decoding_with_a_wrong_secret_fails(self) -> None:
        with self.assertRaises(jwt.InvalidSignatureError):
            jwt.decode(
                create_access_token(1),
                "not-the-secret",
                algorithms=[settings.JWT_ALGORITHM],
            )

    def test_the_algorithm_is_pinned_so_none_tokens_are_refused(self) -> None:
        """`decode` 传了 `algorithms=[...]`，所以 alg=none 的伪造 token 进不来。"""
        unsigned = jwt.encode({"user_id": 1}, key="", algorithm="none")
        with self.assertRaises(jwt.InvalidAlgorithmError):
            decode_access_token(unsigned)


class PasswordUtilsCases(unittest.TestCase):
    def test_a_hash_is_salted_and_never_equals_the_plaintext(self) -> None:
        first = hash_password("123456")
        second = hash_password("123456")
        self.assertNotEqual(first, "123456")
        self.assertNotEqual(first, second)  # 同一个密码两次哈希不同
        self.assertTrue(first.startswith("$2b$"))

    def test_verify_accepts_the_right_password_and_rejects_others(self) -> None:
        hashed = hash_password("123456")
        self.assertTrue(verify_password("123456", hashed))
        self.assertFalse(verify_password("1234567", hashed))
        self.assertFalse(verify_password("", hashed))

    def test_verify_is_case_and_unicode_exact(self) -> None:
        hashed = hash_password("密碼Aa")
        self.assertTrue(verify_password("密碼Aa", hashed))
        self.assertFalse(verify_password("密碼aa", hashed))

    def test_an_empty_password_can_still_be_hashed(self) -> None:
        hashed = hash_password("")
        self.assertTrue(verify_password("", hashed))

    def test_verify_rejects_a_malformed_hash(self) -> None:
        self.assertFalse(verify_password("123456", "definitely-not-a-bcrypt-hash"))

    def test_a_password_longer_than_72_bytes_is_refused(self) -> None:
        """工具函数也必须守住 bcrypt 的 72 字节边界。"""
        with self.assertRaises(ValueError):
            hash_password("a" * 73)

    def test_a_72_byte_password_is_the_boundary(self) -> None:
        hashed = hash_password("a" * 72)
        self.assertTrue(verify_password("a" * 72, hashed))


if __name__ == "__main__":
    unittest.main()
