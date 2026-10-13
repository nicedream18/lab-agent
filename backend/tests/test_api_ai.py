"""AI 对话接口（app/api/ai.py）与它的入口服务（app/services/agent_service.py）。

这是全站唯一「不能真的跑」的接口：它会去调大模型。所以这里的策略是
**用替身换掉 agent_service 的两个入口**（run_agent / stream_agent），
只验证接口层自己负责的三件事：

  1. 请求/响应的形状（ChatMessage / conversation_id 的来回）
  2. SSE 帧的格式（`data: ` 前缀 + `\\n\\n` 结尾 + 中文不转义 + 响应头）
  3. 错误怎么变成「能读的中文」

另外还覆盖了两个纯函数（`_last_user_query` / `_history_from_request`）——
它们是跑在 LLM 之前的守卫，可以用真实实现验证，完全不联网。
"""

from __future__ import annotations

import json
import re
import unittest
from unittest import mock

import app.models  # noqa: F401
from tests.api_harness import ApiTestCase

from app.api.ai import _sse_line
from app.common.exceptions import BusinessException
from app.schemas.ai import ChatMessage, ChatRequest
from app.services import agent_service

CHAT = "/api/ai/chat"
STREAM = "/api/ai/chat/stream"
CONVERSATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _fake_stream_agent(events: list[dict]):
    """把一串事件包成 stream_agent 需要的「异步生成器」。"""

    async def _gen(db, current_user, data):
        for event in events:
            yield event

    return _gen


class SseLineCases(ApiTestCase):
    def test_a_frame_starts_with_data_and_ends_with_a_blank_line(self) -> None:
        line = _sse_line({"type": "token", "text": "你"})
        self.assertTrue(line.startswith("data: "))
        self.assertTrue(line.endswith("\n\n"))
        self.assertEqual(line.count("\n"), 2)

    def test_chinese_is_not_escaped_into_unicode_sequences(self) -> None:
        """ensure_ascii=False：前端拿到的应该是能直接看的中文。"""
        line = _sse_line({"message": "正在理解您的需求…"})
        self.assertIn("正在理解您的需求…", line)
        self.assertNotIn("\\u", line)

    def test_the_payload_is_one_json_object(self) -> None:
        payload = {"type": "done", "answer": "好了", "n": 3, "ok": True, "x": None}
        body = _sse_line(payload)[len("data: ") : -2]
        self.assertEqual(json.loads(body), payload)

    def test_newlines_inside_a_value_do_not_break_the_frame(self) -> None:
        """多行文本必须被 JSON 转义成 `\\n`（两个字符），否则 SSE 的帧边界就乱了。"""
        line = _sse_line({"answer": "第一行\n第二行"})
        self.assertEqual(line.count("\n"), 2)  # 只有结尾那两个换行是真换行
        self.assertIn("\\n", line)  # 值里的换行是转义后的字面量
        self.assertEqual(json.loads(line[len("data: ") : -2])["answer"], "第一行\n第二行")


class LastUserQueryCases(ApiTestCase):
    """跑在 LLM 之前的那道守卫，用真实实现验证。"""

    def test_it_takes_the_last_user_message(self) -> None:
        data = ChatRequest(
            messages=[
                ChatMessage(role="user", content="你好"),
                ChatMessage(role="assistant", content="您好，请问需要什么帮助？"),
                ChatMessage(role="user", content="帮我约明天的计算机实验室"),
            ]
        )
        self.assertEqual(agent_service._last_user_query(data), "帮我约明天的计算机实验室")

    def test_it_skips_blank_user_messages(self) -> None:
        data = ChatRequest(
            messages=[
                ChatMessage(role="user", content="查一下预约"),
                ChatMessage(role="user", content="   "),
            ]
        )
        self.assertEqual(agent_service._last_user_query(data), "查一下预约")

    def test_it_strips_surrounding_whitespace(self) -> None:
        data = ChatRequest(messages=[ChatMessage(role="user", content="  你好  ")])
        self.assertEqual(agent_service._last_user_query(data), "你好")

    def test_no_usable_user_message_is_a_business_error(self) -> None:
        for messages in (
            [],
            [ChatMessage(role="assistant", content="我先说一句")],
            [ChatMessage(role="user", content="  ")],
        ):
            with self.subTest(messages=messages):
                with self.assertRaises(BusinessException) as ctx:
                    agent_service._last_user_query(ChatRequest(messages=messages))
                self.assertEqual(ctx.exception.message, "请输入您要对话的内容")


class HistoryCases(ApiTestCase):
    def test_the_current_question_is_not_repeated_inside_the_history(self) -> None:
        data = ChatRequest(
            messages=[
                ChatMessage(role="user", content="上午好"),
                ChatMessage(role="assistant", content="上午好，需要什么帮助？"),
                ChatMessage(role="user", content="帮我约实验室"),
            ]
        )
        history = agent_service._history_from_request(data, "帮我约实验室")
        self.assertEqual(
            history,
            [
                {"role": "user", "content": "上午好"},
                {"role": "assistant", "content": "上午好，需要什么帮助？"},
            ],
        )

    def test_an_empty_history_is_reported_as_none(self) -> None:
        """返回 None 而不是 [] —— 调用方用「有没有传」来决定是否回落到进程内记忆。"""
        data = ChatRequest(messages=[ChatMessage(role="user", content="只有这一句")])
        self.assertIsNone(agent_service._history_from_request(data, "只有这一句"))

    def test_blank_and_unknown_roles_are_dropped(self) -> None:
        data = ChatRequest(
            messages=[
                ChatMessage(role="system", content="我是系统提示"),
                ChatMessage(role="user", content="  "),
                ChatMessage(role="user", content="真问题"),
            ]
        )
        self.assertIsNone(agent_service._history_from_request(data, "真问题"))

    def test_history_is_capped(self) -> None:
        from app.agent.state import MAX_HISTORY_MESSAGES

        messages = [ChatMessage(role="user", content=f"第{i}句") for i in range(1, 60)]
        data = ChatRequest(messages=messages)
        history = agent_service._history_from_request(data, "不是任何一句")
        self.assertEqual(len(history or []), MAX_HISTORY_MESSAGES)
        self.assertEqual(history[-1]["content"], "第59句")  # 留最近的那一批


class EnsureConversationIdCases(ApiTestCase):
    def test_a_missing_id_is_generated(self) -> None:
        data = ChatRequest(messages=[ChatMessage(role="user", content="你好")])
        self.assertTrue(CONVERSATION_ID_RE.match(agent_service.ensure_conversation_id(data)))
        # 关键副作用：必须写回请求对象，路由就靠它把 id 下发给前端
        self.assertEqual(data.conversation_id, agent_service.ensure_conversation_id(data))

    def test_a_supplied_id_is_kept_verbatim(self) -> None:
        data = ChatRequest(
            messages=[ChatMessage(role="user", content="你好")], conversation_id="abc123"
        )
        self.assertEqual(agent_service.ensure_conversation_id(data), "abc123")

    def test_a_blank_id_is_treated_as_missing(self) -> None:
        data = ChatRequest(
            messages=[ChatMessage(role="user", content="你好")], conversation_id="   "
        )
        self.assertTrue(CONVERSATION_ID_RE.match(agent_service.ensure_conversation_id(data)))

    def test_calling_it_twice_does_not_rotate_the_id(self) -> None:
        data = ChatRequest(messages=[ChatMessage(role="user", content="你好")])
        self.assertEqual(
            agent_service.ensure_conversation_id(data), agent_service.ensure_conversation_id(data)
        )


class ChatCases(ApiTestCase):
    """POST /api/ai/chat —— 非流式入口。"""

    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")

    def _post(self, body: dict, user=None):
        return self.client.post(CHAT, json=body, headers=self.auth(user or self.student))

    def test_a_normal_answer_is_wrapped_as_an_assistant_message(self) -> None:
        async def fake_run_agent(db, current_user, data):
            agent_service.ensure_conversation_id(data)
            return "明天上午 9 点计算机实验室还有位置"

        with mock.patch.object(agent_service, "run_agent", new=fake_run_agent):
            data = self.assert_success(
                self._post({"messages": [{"role": "user", "content": "帮我约实验室"}]})
            )

        self.assertEqual(data["role"], "assistant")
        self.assertEqual(data["content"], "明天上午 9 点计算机实验室还有位置")
        self.assertTrue(CONVERSATION_ID_RE.match(data["conversation_id"]))

    def test_a_supplied_conversation_id_comes_back_unchanged(self) -> None:
        async def fake_run_agent(db, current_user, data):
            agent_service.ensure_conversation_id(data)
            return "好"

        with mock.patch.object(agent_service, "run_agent", new=fake_run_agent):
            data = self.assert_success(
                self._post(
                    {
                        "messages": [{"role": "user", "content": "继续"}],
                        "conversation_id": "session-42",
                    }
                )
            )
        self.assertEqual(data["conversation_id"], "session-42")

    def test_the_route_trusts_run_agent_to_backfill_the_conversation_id(self) -> None:
        """路由自己不生成 id，它只是把 `data.conversation_id` 原样回传。

        也就是说 `conversation_id` 一定要由 run_agent 负责填 ——
        这条测试把这个隐式契约写下来了：替身不回填，响应里就是 null
        （不是「没有这个字段」）。
        """

        async def stub_run_agent(db, current_user, data):
            return "好"

        with mock.patch.object(agent_service, "run_agent", new=stub_run_agent):
            data = self.assert_success(
                self._post({"messages": [{"role": "user", "content": "你好"}]})
            )
        self.assertIsNone(data["conversation_id"])

    def test_the_current_user_is_passed_through_to_the_service(self) -> None:
        seen: list[int] = []

        async def fake_run_agent(db, current_user, data):
            seen.append(current_user.id)
            return "好"

        with mock.patch.object(agent_service, "run_agent", new=fake_run_agent):
            self._post({"messages": [{"role": "user", "content": "你好"}]})
        self.assertEqual(seen, [self.student.id])

    def test_a_business_error_becomes_a_readable_message(self) -> None:
        async def fake_run_agent(db, current_user, data):
            raise BusinessException(message="Agent 没有返回任何内容，请换一种说法再试")

        with mock.patch.object(agent_service, "run_agent", new=fake_run_agent):
            response = self._post({"messages": [{"role": "user", "content": "你好"}]})
        self.assert_business_error(response, "Agent 没有返回任何内容，请换一种说法再试")

    def test_an_empty_question_is_rejected_before_any_model_call(self) -> None:
        """真实实现：`_last_user_query` 在调模型之前就抛错，所以这条用例不联网。"""
        response = self._post({"messages": [{"role": "assistant", "content": "我先说一句"}]})
        # 「没有 id 的会话」这种请求走的路径也一样
        self.assert_business_error(response, "请输入您要对话的内容")

    def test_an_empty_message_list_is_also_rejected(self) -> None:
        self.assert_business_error(
            self._post({"messages": []}), "请输入您要对话的内容"
        )

    def test_chat_requires_authentication(self) -> None:
        self.assert_unauthorized(
            self.client.post(CHAT, json={"messages": [{"role": "user", "content": "你好"}]})
        )

    def test_messages_are_required(self) -> None:
        self.assert_validation_error(self._post({}))

    def test_each_message_needs_a_role_and_a_content(self) -> None:
        self.assert_validation_error(self._post({"messages": [{"content": "只有内容"}]}))
        self.assert_validation_error(self._post({"messages": [{"role": "user"}]}))

    def test_unknown_message_fields_are_ignored(self) -> None:
        async def fake_run_agent(db, current_user, data):
            agent_service.ensure_conversation_id(data)
            return "好"

        with mock.patch.object(agent_service, "run_agent", new=fake_run_agent):
            self.assert_success(
                self._post(
                    {
                        "messages": [
                            {"role": "user", "content": "你好", "evil": True, "id": 1}
                        ],
                        "extra_top_level": "ignore me",
                    }
                )
            )


class ChatStreamCases(ApiTestCase):
    """POST /api/ai/chat/stream —— SSE。"""

    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")

    def _post(self, body: dict, user=None):
        return self.client.post(STREAM, json=body, headers=self.auth(user or self.student))

    @staticmethod
    def _frames(body: str) -> list[dict]:
        frames = []
        for chunk in body.split("\n\n"):
            chunk = chunk.strip()
            if chunk:
                assert chunk.startswith("data: "), chunk
                frames.append(json.loads(chunk[len("data: ") :]))
        return frames

    def test_each_event_is_serialised_as_one_sse_frame(self) -> None:
        events = [
            {"type": "session", "conversation_id": "c1"},
            {"type": "status", "message": "正在理解您的需求…"},
            {"type": "node", "node": "analyze", "status": "done"},
            {"type": "token", "text": "已经帮您"},
            {"type": "done", "answer": "已经帮您约好了"},
        ]
        with mock.patch.object(agent_service, "stream_agent", new=_fake_stream_agent(events)):
            response = self._post({"messages": [{"role": "user", "content": "约实验室"}]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._frames(response.text), events)
        # 每条事件后面都有一个空行分隔，末尾也是
        self.assertTrue(response.text.endswith("\n\n"))

    def test_the_stream_headers_are_sse_friendly(self) -> None:
        with mock.patch.object(agent_service, "stream_agent", new=_fake_stream_agent([{"type": "done"}])):
            response = self._post({"messages": [{"role": "user", "content": "你好"}]})

        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(response.headers["connection"], "keep-alive")
        # Nginx 默认会缓冲整段，不关掉的话前端看不到「逐字出现」的效果
        self.assertEqual(response.headers["x-accel-buffering"], "no")

    def test_chinese_text_is_not_escaped_in_the_stream(self) -> None:
        with mock.patch.object(
            agent_service, "stream_agent", new=_fake_stream_agent([{"type": "token", "text": "好的"}])
        ):
            response = self._post({"messages": [{"role": "user", "content": "你好"}]})
        self.assertIn("好的", response.text)
        self.assertNotIn("\\u", response.text)

    def test_a_non_user_first_message_yields_a_single_error_frame(self) -> None:
        """用**真实**的 stream_agent：守卫在调模型之前抛错，所以这条也不联网。

        注意这时候是 HTTP 200 + 一帧 error —— 流式接口已经把状态码发出去了，
        没法再改成 500，只能把错误当成一个事件往下带。前端必须认这个形态。
        """
        response = self._post({"messages": [{"role": "assistant", "content": "我先说一句"}]})
        self.assertEqual(response.status_code, 200)
        frames = self._frames(response.text)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["type"], "error")
        self.assertEqual(frames[0]["message"], "请输入您要对话的内容")

    def test_streaming_requires_authentication(self) -> None:
        self.assert_unauthorized(
            self.client.post(STREAM, json={"messages": [{"role": "user", "content": "你好"}]})
        )

    def test_the_stream_endpoint_takes_a_post_body_not_a_query(self) -> None:
        """GET 打同一个路径要 405（说明它确实只注册了 POST）。"""
        response = self.client.get(STREAM, headers=self.auth(self.student))
        self.assertEqual(response.status_code, 405)

    def test_an_empty_stream_still_returns_a_valid_response(self) -> None:
        with mock.patch.object(agent_service, "stream_agent", new=_fake_stream_agent([])):
            response = self._post({"messages": [{"role": "user", "content": "你好"}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "")


if __name__ == "__main__":
    unittest.main()
