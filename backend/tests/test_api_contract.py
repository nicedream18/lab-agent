"""接口层「契约」测试：路由前缀、鉴权闸门、统一响应体、全局异常处理器、CORS。

这一层不测业务，只测那些**一旦坏掉就全站坏掉**的横切约定：
  - 所有业务路由是不是都在 /api 前缀下
  - 除了三个公开接口，是不是每个接口都真的挡住了没带 token 的请求
  - 四类异常处理器吐出来的到底是 HTTP 200 还是 401/422/500，body 长什么样
  - CORS 只对有白名单的 Origin 放行
"""

from __future__ import annotations

import re
import unittest
from unittest.mock import patch

import app.models  # noqa: F401
from tests.api_harness import ApiTestCase

from app.api import api as api_router
from app.common.exceptions import global_excpetion_hadler
from app.main import app as application
from app.models.user import User

# 有意不加鉴权的公开接口：登录、注册、文件上传。
PUBLIC_PATHS = {"/api/auth/login", "/api/auth/register", "/api/files/upload"}

ALLOWED_ORIGIN = "http://localhost:5173"


def _concrete_path(path: str) -> str:
    """把 /api/user/{user_id} 这种模板路径填成可请求的实参路径。"""
    return re.sub(r"\{[^}]*\}", "1", path)


class RouteContractCases(ApiTestCase):
    def test_all_business_routes_live_under_the_api_prefix(self) -> None:
        paths = [route.path for route in api_router.routes if route.methods]
        self.assertTrue(paths)
        offenders = [path for path in paths if not path.startswith("/api")]
        self.assertEqual(offenders, [])

        # 唯一不在 /api 下的路由是根路由，它属于 app 而不属于任何子路由
        self.assertEqual(self.client.get("/").json(), {"message": "Hello FastAPI"})

    def test_view_route_without_trailing_slash_redirects(self) -> None:
        # 子路由用 prefix="/lab" + @router.get("/list")，所以真实路径是 /api/lab/list。
        # 写错成 /api/lab/ 时 Starlette 会 307 到正确路径，前端不用改也得能用。
        response = self.client.get("/api/lab/list/", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertTrue(response.headers["location"].endswith("/api/lab/list"))

    def test_every_protected_route_rejects_a_request_without_token(self) -> None:
        """逐个打一遍所有业务接口，确认没有漏挂鉴权依赖的。

        鉴权依赖比 body 校验先跑，所以即便 body 是空的，拿到的也应该是 401
        而不是 422 —— 这个顺序本身也是被测的一部分。
        """
        checked: list[str] = []
        for route in api_router.routes:
            if not route.methods or route.path in PUBLIC_PATHS:
                continue
            path = _concrete_path(route.path)
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                with self.subTest(method=method, path=path):
                    response = self.client.request(
                        method,
                        path,
                        json={},
                        follow_redirects=False,
                    )
                    self.assert_unauthorized(response, "Not authenticated")
                checked.append(f"{method} {path}")

        # 防呆：上面的循环要是因为路径拼错一条都没跑到，这条会失败
        self.assertGreater(len(checked), 20)

    def test_the_three_intentionally_public_routes_are_reachable_without_token(self) -> None:
        response = self.client.post(
            "/api/auth/register",
            json={"username": "open-route", "password": "pw-123456"},
        )
        self.assert_success(response, message="注册成功")

        response = self.client.post(
            "/api/auth/login", json={"username": "open-route", "password": "pw-123456"}
        )
        self.assert_success(response, message="登录成功")

        # 文件上传刻意没有鉴权（见 app/api/files.py）。这条用例不是为了证明它
        # 「对」，而是把这个事实钉住：哪天要加鉴权，会在这里失败并被看见。
        response = self.client.post(
            "/api/files/upload",
            files={"file": ("a.txt", b"hello", "text/plain")},
        )
        self.assert_business_error(response, "不支持的文件后缀：.txt")


class ExceptionHandlerCases(ApiTestCase):
    """错误处理器统一封装业务、鉴权、校验及路由错误。"""

    def setUp(self) -> None:
        super().setUp()
        self.admin = self.make_admin()
        self.student = self.make_user()

    def test_unauthenticated_request_is_a_real_401_with_the_unified_body(self) -> None:
        body = self.assert_unauthorized(self.client.get("/api/user/info"))
        self.assertIsNone(body["data"])

    def test_token_of_a_nonexistent_user_is_401_not_500(self) -> None:
        # 账号被删之后旧 token 还没过期，是很常见的场景：必须是 401 而不是崩成 500
        response = self.client.get(
            "/api/user/info", headers=self.expired_auth(self.student)
        )
        self.assert_unauthorized(response, "无效的登录凭证")

    def test_disabled_account_is_401(self) -> None:
        disabled = self.make_user("banned", status=0)
        response = self.client.get("/api/user/info", headers=self.auth(disabled))
        self.assert_unauthorized(response, "用户被禁用")

    def test_garbage_token_is_401(self) -> None:
        response = self.client.get(
            "/api/user/info", headers={"Authorization": "Bearer not-a-jwt"}
        )
        self.assert_unauthorized(response, "登录已失效，请重新登录")

    def test_non_admin_hitting_an_admin_route_is_http_403(self) -> None:
        response = self.client.get("/api/user/list", headers=self.auth(self.student))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)
        self.assertEqual(response.json()["message"], "无权限访问")

    def test_business_exception_returns_http_200_with_code_500(self) -> None:
        # 实验室不存在 → BusinessException，默认 code=500，HTTP 仍是 200
        response = self.client.put("/api/lab/99999", json={}, headers=self.auth(self.admin))
        self.assert_business_error(response, "实验室不存在")

    def test_business_exception_can_carry_a_custom_code(self) -> None:
        # 记忆不存在 → BusinessException(code=404)，HTTP 依然是 200
        response = self.client.delete("/api/agent/memory/99999", headers=self.auth(self.student))
        self.assert_business_error(response, "记忆不存在", code=404)

    def test_validation_error_is_422_with_a_fixed_message(self) -> None:
        # 校验失败不把 Pydantic 的细节吐给前端，统一成一句话
        response = self.client.post("/api/auth/login", json={})
        self.assert_validation_error(response)

    def test_validation_error_wins_over_the_endpoint(self) -> None:
        # code 超长（>6）会先被 Pydantic 拦住，端点的业务逻辑根本没跑
        response = self.client.put(
            "/api/lab/1", json={"capacity": "不是数字"}, headers=self.auth(self.admin)
        )
        self.assert_validation_error(response)

    def test_unhandled_exception_is_500_with_a_masked_message(self) -> None:
        """全局兜底：任何没被上面三类接住的异常都变成 500 + 「服务器内部错误」。

        刻意不把堆栈或异常原文回给前端 —— 那属于信息泄露。
        """
        with patch(
            "app.services.lab_service.get_lab_page_list",
            side_effect=RuntimeError("数据库连接池炸了"),
        ):
            response = self.raw_client().get("/api/lab/list", headers=self.auth(self.student))

        self.assertEqual(response.status_code, 500, response.text)
        body = response.json()
        self.assertEqual(body["code"], 500, body)
        self.assertEqual(body["message"], "服务器内部错误", body)
        self.assertNotIn("数据库连接池炸了", response.text)

    def test_the_projects_global_handler_is_the_one_that_owns_the_500_branch(self) -> None:
        """Exception 这个兜底必须指向项目自己的处理器。

        FastAPI 构建中间件栈时会把 exception_handlers 里 Exception/500 那一项
        单独挑出来当兜底（ServerErrorMiddleware 只吃一个处理器），所以后来注册的
        会覆盖先注册的 —— 万一有人往 main.py 后面再塞一个 Exception 处理器，
        这个断言就会失败，从而暴露「兜底被换掉了」。
        """
        self.assertIs(application.exception_handlers[Exception], global_excpetion_hadler)
        self.assertIsNot(application.exception_handlers[Exception], None)

    def test_business_handler_owns_the_business_exception_branch(self) -> None:
        from app.common.exceptions import (
            BusinessException,
            bussiness_excpetion_hadler,
        )

        self.assertIs(
            application.exception_handlers[BusinessException], bussiness_excpetion_hadler
        )

    def test_validation_handler_owns_the_request_validation_branch(self) -> None:
        from fastapi.exceptions import RequestValidationError

        from app.common.exceptions import validation_excpetion_hadler

        self.assertIs(
            application.exception_handlers[RequestValidationError],
            validation_excpetion_hadler,
        )

    def test_unknown_path_and_method_use_the_unified_envelope(self) -> None:
        not_found = self.client.get("/api/does-not-exist")
        self.assertEqual(not_found.status_code, 404, not_found.text)
        self.assertEqual(not_found.json(), {"code": 404, "message": "Not Found", "data": None})

        # 方法不对（GET 一个 POST-only 的接口）也走统一响应体。
        wrong_method = self.client.get("/api/auth/login")
        self.assertEqual(wrong_method.status_code, 405, wrong_method.text)
        self.assertEqual(wrong_method.json(), {"code": 405, "message": "Method Not Allowed", "data": None})


class CorsCases(ApiTestCase):
    def test_preflight_from_an_allowed_origin_is_accepted(self) -> None:
        response = self.client.options(
            "/api/lab/list",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], ALLOWED_ORIGIN)
        self.assertEqual(response.headers["access-control-allow-credentials"], "true")
        self.assertIn("GET", response.headers["access-control-allow-methods"])

    def test_an_unknown_origin_gets_no_cors_header(self) -> None:
        response = self.client.get("/api/lab/list", headers={"Origin": "http://evil.example"})
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_error_responses_also_carry_cors_headers(self) -> None:
        # 401 也必须带 CORS 头，否则前端连错误码都读不到，只会看到一个跨域报错
        response = self.client.get("/api/lab/list", headers={"Origin": ALLOWED_ORIGIN})
        self.assert_unauthorized(response, "Not authenticated")
        self.assertEqual(response.headers["access-control-allow-origin"], ALLOWED_ORIGIN)


class SchemaCases(ApiTestCase):
    def test_openapi_document_is_served(self) -> None:
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        # 抽查几条关键路径，防止哪天 include_router 被漏掉
        for path in (
            "/api/auth/login",
            "/api/reservation",
            "/api/agent/trace/{conversation_id}",
            "/api/ai/chat/stream",
        ):
            self.assertIn(path, paths)

    def test_extra_body_fields_are_silently_ignored(self) -> None:
        """所有请求体模型都没开 extra='forbid'。

        这既是兼容性（前端多传一个字段不会 422），也是安全边界：
        多余字段永远进不了 model_dump()，塞不进数据库。
        """
        response = self.client.post(
            "/api/auth/register",
            json={
                "username": "extra-fields",
                "password": "pw-123456",
                "role": "admin",  # 注册接口刻意不认这个字段
                "status": 0,
                "id": 424242,
            },
        )
        self.assert_success(response, message="注册成功")

        created = self.db.query(User).filter_by(username="extra-fields").first()
        self.assertIsNotNone(created)
        # 注册出来的账号永远是 student / status=1，传什么都不改
        self.assertEqual(created.role, "student")
        self.assertEqual(created.status, 1)
        self.assertNotEqual(created.id, 424242)


if __name__ == "__main__":
    unittest.main()
