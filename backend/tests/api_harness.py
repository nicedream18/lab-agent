"""HTTP API test harness: real routes and JWT with an isolated SQLite database."""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Any

from evals.offline_env import configure_offline

# 必须排在任何 app.* 导入之前：它把 DATABASE_URL / JWT_SECRET_KEY 等必填配置塞进
# os.environ。os.environ 的优先级高于 backend/.env 这个 dotenv 源，所以即便本机
# 存在 .env（指向真实 MySQL），app.database.engine 也会被指到内存 SQLite ——
# 接口层用例绝不会碰到真实库。
configure_offline()

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

import app.models  # noqa: E402,F401  导入副作用：把所有表注册进 Base.metadata
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.ai_conversation import AiConversation  # noqa: E402
from app.models.ai_message import AiMessage  # noqa: E402
from app.models.agent_tool_execution import AgentToolExecution  # noqa: E402
from app.models.agent_trace import AgentTrace  # noqa: E402
from app.models.equipment import Equipment  # noqa: E402
from app.models.lab import Lab  # noqa: E402
from app.models.reservation import Reservation  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.user_memory import UserMemory  # noqa: E402
from app.utils.jwt import create_access_token  # noqa: E402
from app.utils.password import hash_password  # noqa: E402

# 用例里反复出现的固定密码。bcrypt 一次约 200ms，所以用例里尽量只建必要的用户。
DEFAULT_PASSWORD = "pw-123456"

# 统一响应体的成功码
SUCCESS_CODE = 200


class ApiTestCase(unittest.TestCase):
    """接口层用例基类。

    三处刻意为之的设计，都是踩过坑换来的：

    1. 用**临时文件** SQLite，不用 ``sqlite:///:memory:``
       TestClient 把同步端点、以及同步依赖 get_db 都丢进 anyio 的线程池执行。
       内存库每条连接都是各自独立的空库 —— 主线程 create_all 建的表，请求线程
       根本看不到，表现成一片「no such table」。文件库 + check_same_thread=False
       才能跨线程共享。

    2. 用 dependency_overrides[get_db] 换会话工厂，而不是去改 app.database.engine
       引擎是模块导入时按 settings.DATABASE_URL 建好的，改它会影响其它用例。
       换依赖能让每个用例拿到一个干净库，互相不污染。

    3. TestClient 故意**不进 with**
       一旦进 with 就会跑 lifespan，那会启动「过期预约扫描」「会话清理」两个
       无限循环协程，还会在线程里加载嵌入模型（约 10s、约 500MB）。
       接口层用例只关心路由、依赖和响应体，不需要后台任务。
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

        url = "sqlite+pysqlite:///" + str(Path(self._tmpdir.name) / "api-test.db")
        self.engine = create_engine(url, connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)

        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.SessionLocal()
        self.addCleanup(self.db.close)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.addCleanup(app.dependency_overrides.clear)

        self.client = TestClient(app)

    # ------------------------------------------------------------------
    # 客户端
    # ------------------------------------------------------------------

    def _patch(self, target: Any, attribute: str, value: Any) -> None:
        """临时替换某个模块级名字，用例结束自动还原。

        接口层用的是模块级常量（`files.UPLOAD_DIR`、`files.MAX_FILE_SIZE`…），
        这些常量是在 import 时从 config 拷进模块命名空间的，
        所以要改就得改 `app.api.xxx` 这一层，改 `app.config` 没用。
        """
        patcher = mock.patch.object(target, attribute, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def raw_client(self) -> TestClient:
        """不把服务端异常重新抛出的客户端。

        全局异常处理器那条链是「先构造 500 响应，再把原异常重新抛出」，
        所以默认的 TestClient 会在**测试代码**里直接看到那个异常。
        要断言 500 的响应体，只能用这个客户端。
        """
        return TestClient(app, raise_server_exceptions=False)

    # ------------------------------------------------------------------
    # 造数据
    # ------------------------------------------------------------------

    def make_user(
        self,
        username: str = "student",
        *,
        role: str = "student",
        status: int = 1,
        password: str = DEFAULT_PASSWORD,
        name: str | None = None,
    ) -> User:
        user = User(
            username=username,
            password=hash_password(password),
            name=name or username,
            role=role,
            status=status,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def make_admin(
        self,
        username: str = "admin",
        *,
        status: int = 1,
        password: str = DEFAULT_PASSWORD,
        name: str = "管理员",
    ) -> User:
        return self.make_user(
            username, role="admin", status=status, password=password, name=name
        )

    def make_lab(
        self,
        name: str = "计算机实验室",
        *,
        status: int = 1,
        open_time: str = "08:00",
        close_time: str = "20:00",
        location: str = "A座101",
        capacity: int = 30,
        description: str = "用于上机实验",
    ) -> Lab:
        lab = Lab(
            name=name,
            status=status,
            open_time=open_time,
            close_time=close_time,
            location=location,
            capacity=capacity,
            description=description,
        )
        self.db.add(lab)
        self.db.commit()
        self.db.refresh(lab)
        return lab

    def make_equipment(
        self,
        lab: Lab,
        name: str = "开发主机",
        *,
        status: int = 1,
        quantity: int = 1,
        spec: str | None = None,
    ) -> Equipment:
        equipment = Equipment(
            lab_id=lab.id,
            name=name,
            status=status,
            quantity=quantity,
            spec=spec,
            description="",
        )
        self.db.add(equipment)
        self.db.commit()
        self.db.refresh(equipment)
        return equipment

    def make_reservation(
        self,
        user: User,
        lab: Lab,
        *,
        date: str = "2099-10-13",
        start_time: str = "09:00",
        end_time: str = "11:00",
        status: int = 0,
        equipment: Equipment | None = None,
        remark: str | None = None,
    ) -> Reservation:
        reservation = Reservation(
            user_id=user.id,
            lab_id=lab.id,
            equipment_id=equipment.id if equipment is not None else None,
            date=date,
            start_time=start_time,
            end_time=end_time,
            status=status,
            remark=remark,
        )
        self.db.add(reservation)
        self.db.commit()
        self.db.refresh(reservation)
        return reservation

    def make_memory(
        self, user: User, content: str, *, memory_type: str = "preference", hit_count: int = 0
    ) -> UserMemory:
        row = UserMemory(
            user_id=user.id,
            memory_type=memory_type,
            content=content,
            hit_count=hit_count,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def make_conversation(
        self,
        user: User,
        conversation_id: str,
        *,
        title: str = "帮我约明天的计算机实验室",
        message_count: int = 0,
    ) -> AiConversation:
        from datetime import datetime

        row = AiConversation(
            user_id=user.id,
            conversation_id=conversation_id,
            title=title,
            message_count=message_count,
            last_time=datetime.now(),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def make_message(self, user: User, conversation_id: str, role: str, content: str) -> AiMessage:
        row = AiMessage(
            user_id=user.id, conversation_id=conversation_id, role=role, content=content
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def make_trace(
        self,
        user: User,
        conversation_id: str,
        *,
        node_name: str = "analyze",
        status: str = "success",
        execution_time: float = 1.0,
        output: str | None = "{}",
    ) -> AgentTrace:
        row = AgentTrace(
            user_id=user.id,
            conversation_id=conversation_id,
            node_name=node_name,
            step_index=0,
            status=status,
            input="问一句",
            output=output,
            execution_time=execution_time,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def make_tool_execution(
        self,
        user: User,
        conversation_id: str,
        *,
        tool_name: str = "query_user_reservations",
        is_write: int = 0,
        status: str = "success",
        execution_time: float = 2.0,
    ) -> AgentToolExecution:
        row = AgentToolExecution(
            user_id=user.id,
            conversation_id=conversation_id,
            step_index=1,
            tool_name=tool_name,
            tool_label=tool_name,
            tool_input="{}",
            tool_output="{}",
            observation="",
            is_write=is_write,
            status=status,
            execution_time=execution_time,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    # ------------------------------------------------------------------
    # 请求
    # ------------------------------------------------------------------

    def auth(self, user: User) -> dict[str, str]:
        """真实签发一个 JWT，模拟前端的 Authorization 头。"""
        return {"Authorization": f"Bearer {create_access_token(user.id)}"}

    def expired_auth(self, user: User) -> dict[str, str]:
        """用一个不属于任何用户的 user_id 签 token（模拟账号被删 / 伪造凭证）。"""
        return {"Authorization": f"Bearer {create_access_token(user.id + 9999)}"}

    def reload(self, obj: Any) -> Any:
        """端点在自己的会话里改了库，本地会话的身份映射是脏的，先全部过期再读。"""
        self.db.expire_all()
        return obj

    # ------------------------------------------------------------------
    # 断言
    # ------------------------------------------------------------------

    def assert_success(
        self, response, *, message: str | None = None, status: int = 200
    ) -> Any:
        """断言统一响应体的成功分支，返回 body["data"]。"""
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(body["code"], SUCCESS_CODE, body)
        if message is not None:
            self.assertEqual(body["message"], message, body)
        self.assertIn("data", body)
        return body["data"]

    def assert_business_error(self, response, message: str, *, code: int = 500) -> dict:
        """业务异常保留既有信封；鉴权拒绝使用真正的 HTTP 403。"""
        self.assertEqual(response.status_code, 403 if code == 403 else 200, response.text)
        body = response.json()
        self.assertEqual(body["code"], code, body)
        self.assertEqual(body["message"], message, body)
        return body

    def assert_unauthorized(self, response, detail: str = "Not authenticated") -> dict:
        """鉴权失败是真的 401（HTTPException 处理器保留原状态码），不是 200。

        注意文案有两个来源：没带 token 是 OAuth2PasswordBearer 的
        「Not authenticated」；带了 token 但解不开/用户不存在是业务自己抛的文案。
        """
        self.assertEqual(response.status_code, 401, response.text)
        body = response.json()
        self.assertEqual(body["code"], 401, body)
        self.assertEqual(body["message"], detail, body)
        return body

    def assert_validation_error(self, response) -> dict:
        """请求参数校验失败：422 + 固定文案。"""
        self.assertEqual(response.status_code, 422, response.text)
        body = response.json()
        self.assertEqual(body["code"], 422, body)
        self.assertEqual(body["message"], "请求参数校验错误", body)
        return body


__all__ = ["ApiTestCase", "SUCCESS_CODE", "DEFAULT_PASSWORD", "Session"]
