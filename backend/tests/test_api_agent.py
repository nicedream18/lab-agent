"""Agent 可观测性接口（app/api/agent.py）。

五组接口，共同的一条底线：**都只能看自己的东西**。
`conversation_id` 是随机的，但「猜不中」不是权限控制 —— 所以每个接口都单独
验一遍跨用户隔离（别人拿到同一个 id 也只能看到空/404）。

另外三处值得钉住的实现细节：
  - /trace 与 /tool-executions 是「先整表捞再在 Python 里过滤 user_id」，
    不是 SQL 层过滤。目前会话量下没问题，钉住它是为了改的时候心里有数。
  - DELETE /conversations/{id} 是幂等的：删不存在的也回 200 + deleted=false，
    刻意不报 404（用户点两次不该看到两次不同结果）。
  - DELETE /memory/{id} 反过来：找不到就 404（code=404 但 HTTP 仍是 200）。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

import app.models  # noqa: F401
from app.agent import memory as agent_memory
from app.agent.state import WRITE_TOOLS
from app.agent.tools import TOOL_REGISTRY, tool_label
from app.models.agent_tool_execution import AgentToolExecution
from app.models.agent_trace import AgentTrace
from app.models.ai_conversation import AiConversation
from app.models.ai_message import AiMessage
from app.models.user_memory import UserMemory

from tests.api_harness import ApiTestCase

TOOLS = "/api/agent/tools"
MEMORY = "/api/agent/memory"
CONVERSATIONS = "/api/agent/conversations"


class TraceCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")
        self.other = self.make_user("lisi")
        self.cid = "conv-trace-1"

    def _trace(self, cid: str, user=None):
        return self.client.get(
            f"/api/agent/trace/{cid}", headers=self.auth(user or self.student)
        )

    def test_a_conversation_trace_is_replayed_in_execution_order(self) -> None:
        self.make_trace(self.student, self.cid, node_name="analyze", execution_time=1.5)
        self.make_trace(self.student, self.cid, node_name="plan", execution_time=2.25)
        self.make_trace(
            self.student, self.cid, node_name="execute", execution_time=0.25
        )

        data = self.assert_success(self._trace(self.cid))
        self.assertEqual(data["conversation_id"], self.cid)
        self.assertEqual(data["total"], 3)
        self.assertEqual(
            [item["node_name"] for item in data["items"]],
            ["analyze", "plan", "execute"],
        )
        self.assertEqual(data["total_time"], 4.0)

    def test_item_fields_are_what_the_trace_panel_needs(self) -> None:
        self.make_trace(
            self.student, self.cid, node_name="analyze", output='{"ok": true}'
        )
        item = self.assert_success(self._trace(self.cid))["items"][0]
        self.assertEqual(item["conversation_id"], self.cid)
        self.assertEqual(item["status"], "success")
        self.assertEqual(item["step_index"], 0)
        self.assertEqual(item["input"], "问一句")
        self.assertEqual(item["output"], '{"ok": true}')
        self.assertIsNotNone(item["create_time"])

    def test_total_time_is_rounded_against_float_noise(self) -> None:
        """0.1 + 0.2 = 0.30000000000000004，接口 must 先 round 再下发。"""
        self.make_trace(self.student, self.cid, execution_time=0.1)
        self.make_trace(self.student, self.cid, execution_time=0.2)
        data = self.assert_success(self._trace(self.cid))
        self.assertEqual(data["total_time"], 0.3)

    def test_another_users_trace_is_invisible(self) -> None:
        self.make_trace(self.other, self.cid, node_name="analyze")
        data = self.assert_success(self._trace(self.cid))
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["items"], [])
        self.assertEqual(data["total_time"], 0.0)

    def test_an_unknown_conversation_is_an_empty_trace_not_a_404(self) -> None:
        data = self.assert_success(self._trace("never-existed"))
        self.assertEqual(
            data,
            {
                "conversation_id": "never-existed",
                "total": 0,
                "total_time": 0.0,
                "items": [],
            },
        )

    def test_a_failed_node_still_shows_up(self) -> None:
        """失败也要能回放 —— 这正是不用「只存好结果」的原因。"""
        self.make_trace(self.student, self.cid, node_name="execute", status="failed")
        item = self.assert_success(self._trace(self.cid))["items"][0]
        self.assertEqual(item["status"], "failed")

    def test_conversation_id_length_is_capped(self) -> None:
        self.assert_validation_error(self._trace("x" * 65))

    def test_trace_requires_authentication(self) -> None:
        self.assert_unauthorized(self.client.get(f"/api/agent/trace/{self.cid}"))


class ToolExecutionCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")
        self.other = self.make_user("lisi")
        self.cid = "conv-tools-1"

    def _executions(self, cid: str, user=None):
        return self.client.get(
            f"/api/agent/tool-executions/{cid}", headers=self.auth(user or self.student)
        )

    def test_a_tool_call_is_replayed_with_its_audit_counters(self) -> None:
        self.make_tool_execution(
            self.student,
            self.cid,
            tool_name="create_reservation",
            is_write=1,
            execution_time=1.25,
        )
        self.make_tool_execution(
            self.student,
            self.cid,
            tool_name="query_user_reservations",
            is_write=0,
            execution_time=0.75,
        )
        self.make_tool_execution(
            self.student,
            self.cid,
            tool_name="cancel_reservation",
            is_write=1,
            status="failed",
            execution_time=2.0,
        )

        data = self.assert_success(self._executions(self.cid))
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["total_time"], 4.0)
        self.assertEqual(data["write_total"], 2)
        self.assertEqual(data["failed_total"], 1)
        self.assertEqual(
            [item["tool_name"] for item in data["items"]],
            ["create_reservation", "query_user_reservations", "cancel_reservation"],
        )

    def test_item_fields_are_what_the_audit_panel_needs(self) -> None:
        self.make_tool_execution(
            self.student, self.cid, tool_name="get_today", is_write=0
        )
        item = self.assert_success(self._executions(self.cid))["items"][0]
        self.assertEqual(item["tool_name"], "get_today")
        self.assertEqual(item["tool_label"], "get_today")
        self.assertEqual(item["tool_input"], "{}")
        self.assertEqual(item["tool_output"], "{}")
        self.assertEqual(item["is_write"], 0)
        self.assertEqual(item["status"], "success")
        self.assertIsNotNone(item["create_time"])

    def test_the_counters_are_zero_for_an_empty_conversation(self) -> None:
        data = self.assert_success(self._executions("nothing-here"))
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["write_total"], 0)
        self.assertEqual(data["failed_total"], 0)
        self.assertEqual(data["items"], [])

    def test_another_users_tool_calls_are_invisible(self) -> None:
        self.make_tool_execution(
            self.other, self.cid, tool_name="create_reservation", is_write=1
        )
        data = self.assert_success(self._executions(self.cid))
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["write_total"], 0)

    def test_conversation_id_length_is_capped(self) -> None:
        self.assert_validation_error(self._executions("x" * 65))

    def test_tool_executions_require_authentication(self) -> None:
        self.assert_unauthorized(
            self.client.get(f"/api/agent/tool-executions/{self.cid}")
        )


class ToolboxCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")

    def _tools(self, user=None):
        return self.client.get(TOOLS, headers=self.auth(user or self.student))

    def test_every_registered_tool_is_exposed(self) -> None:
        data = self.assert_success(self._tools())
        self.assertEqual(len(data), len(TOOL_REGISTRY))
        self.assertEqual({item["name"] for item in data}, set(TOOL_REGISTRY))

    def test_write_tools_are_flagged(self) -> None:
        """前端靠这个标记把「会改你数据」的工具和只读工具区分开。

        `is_write` 来自 `name in WRITE_TOOLS`，而 WRITE_TOOLS 是硬编码的一份集合 ——
        新增写库工具却忘了加进去，界面上就不会有警示标记。这条测试就是在守这个。
        """
        data = self.assert_success(self._tools())
        flagged = {item["name"] for item in data if item["is_write"]}
        self.assertEqual(flagged, set(WRITE_TOOLS))
        self.assertEqual(
            flagged, {"create_reservation", "cancel_reservation", "cancel_reservations"}
        )

    def test_every_entry_has_a_human_readable_label_and_description(self) -> None:
        for item in self.assert_success(self._tools()):
            with self.subTest(tool=item["name"]):
                self.assertTrue(item["label"].strip())
                self.assertNotEqual(item["label"], item["name"])  # 必须是中文别名
                self.assertTrue(item["description"].strip())

    def test_the_label_helper_falls_back_to_the_name(self) -> None:
        self.assertEqual(tool_label("query_user_reservations"), "查询我的预约")
        self.assertEqual(tool_label("no_such_tool"), "no_such_tool")

    def test_the_toolbox_requires_authentication(self) -> None:
        self.assert_unauthorized(self.client.get(TOOLS))


class MemoryReadCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")
        self.other = self.make_user("lisi")

    def _memory(self, user=None):
        return self.client.get(MEMORY, headers=self.auth(user or self.student))

    def test_memories_are_ordered_by_hit_count_then_id(self) -> None:
        self.make_memory(self.student, "很少用到", hit_count=1)
        self.make_memory(self.student, "最常用", hit_count=9)
        self.make_memory(self.student, "中等", hit_count=5)

        data = self.assert_success(self._memory())
        self.assertEqual(
            [item["content"] for item in data], ["最常用", "中等", "很少用到"]
        )

    def test_same_hit_count_falls_back_to_newest_first(self) -> None:
        self.make_memory(self.student, "先写的", hit_count=3)
        self.make_memory(self.student, "后写的", hit_count=3)
        data = self.assert_success(self._memory())
        self.assertEqual([item["content"] for item in data], ["后写的", "先写的"])

    def test_the_response_is_a_bare_list(self) -> None:
        """记忆列表不分页，直接给数组（没有 list/total 外壳）。"""
        data = self.assert_success(self._memory())
        self.assertIsInstance(data, list)
        self.assertEqual(data, [])

    def test_only_my_own_memories_are_listed(self) -> None:
        self.make_memory(self.student, "我的偏好")
        self.make_memory(self.other, "别人的偏好")
        data = self.assert_success(self._memory())
        self.assertEqual([item["content"] for item in data], ["我的偏好"])

    def test_the_list_is_capped_at_fifty(self) -> None:
        for index in range(55):
            self.make_memory(self.student, f"第{index:02d}条", hit_count=index)
        data = self.assert_success(self._memory())
        self.assertEqual(len(data), 50)
        self.assertEqual(data[0]["content"], "第54条")  # hit_count 最高的那条还在

    def test_memory_fields(self) -> None:
        self.make_memory(
            self.student, "喜欢用上午的时间段", memory_type="preference", hit_count=4
        )
        item = self.assert_success(self._memory())[0]
        self.assertEqual(item["content"], "喜欢用上午的时间段")
        self.assertEqual(item["memory_type"], "preference")
        self.assertEqual(item["hit_count"], 4)
        self.assertIsNotNone(item["create_time"])

    def test_memory_requires_authentication(self) -> None:
        self.assert_unauthorized(self.client.get(MEMORY))


class MemoryWriteCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")
        self.other = self.make_user("lisi")

    def test_adding_a_memory(self) -> None:
        response = self.client.post(
            MEMORY,
            json={"content": "我习惯约早上九点"},
            headers=self.auth(self.student),
        )
        body = self.assert_success(response, message="已记住")
        self.assertEqual(body["content"], "我习惯约早上九点")
        self.assertEqual(body["memory_type"], "preference")  # 默认值
        self.assertEqual(body["hit_count"], 0)

        row = self.db.query(UserMemory).one()
        self.assertEqual(row.user_id, self.student.id)

    def test_the_memory_type_can_be_chosen(self) -> None:
        body = self.assert_success(
            self.client.post(
                MEMORY,
                json={"content": "常去计算机实验室", "memory_type": "habit"},
                headers=self.auth(self.student),
            )
        )
        self.assertEqual(body["memory_type"], "habit")

    def test_empty_content_is_a_validation_error(self) -> None:
        response = self.client.post(
            MEMORY, json={"content": ""}, headers=self.auth(self.student)
        )
        self.assert_validation_error(response)
        self.assertEqual(self.db.query(UserMemory).count(), 0)

    def test_whitespace_only_content_is_a_validation_error(self) -> None:
        for content in (" ", "\n", "   "):
            with self.subTest(content=repr(content)):
                response = self.client.post(
                    MEMORY, json={"content": content}, headers=self.auth(self.student)
                )
                self.assert_validation_error(response)
        self.assertEqual(self.db.query(UserMemory).count(), 0)

    def test_writing_the_same_content_again_only_bumps_the_hit_count(self) -> None:
        """`remember` 按内容去重：重复写不新增行，只把 hit_count 加一。"""
        first = self.assert_success(
            self.client.post(
                MEMORY,
                json={"content": "我习惯约早上"},
                headers=self.auth(self.student),
            )
        )
        second = self.assert_success(
            self.client.post(
                MEMORY,
                json={"content": "  我习惯约早上  "},
                headers=self.auth(self.student),
            )
        )
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["hit_count"], first["hit_count"] + 1)
        self.assertEqual(self.db.query(UserMemory).count(), 1)

    def test_over_long_content_is_a_validation_error(self) -> None:
        self.assert_validation_error(
            self.client.post(
                MEMORY, json={"content": "啊" * 501}, headers=self.auth(self.student)
            )
        )
        self.assert_validation_error(
            self.client.post(
                MEMORY,
                json={"content": "正常", "memory_type": "x" * 33},
                headers=self.auth(self.student),
            )
        )

    def test_a_failed_write_is_reported_as_a_readable_error(self) -> None:
        """`remember` 返回 None 表示「写了但没落库」，接口要把它变成提示而不是 500。"""
        with mock.patch.object(agent_memory, "remember", return_value=None):
            response = self.client.post(
                MEMORY, json={"content": "记不住的"}, headers=self.auth(self.student)
            )
        self.assert_business_error(response, "记忆写入失败，请稍后重试")
        self.assertEqual(self.db.query(UserMemory).count(), 0)

    def test_a_new_memory_belongs_to_the_caller(self) -> None:
        self.client.post(
            MEMORY, json={"content": "我的"}, headers=self.auth(self.student)
        )
        self.client.post(
            MEMORY, json={"content": "他的"}, headers=self.auth(self.other)
        )
        mine = self.assert_success(
            self.client.get(MEMORY, headers=self.auth(self.student))
        )
        self.assertEqual([item["content"] for item in mine], ["我的"])

    def test_adding_memory_requires_authentication(self) -> None:
        self.assert_unauthorized(self.client.post(MEMORY, json={"content": "x"}))

    def test_forgetting_my_own_memory(self) -> None:
        row = self.make_memory(self.student, "不该记的")
        response = self.client.delete(
            f"{MEMORY}/{row.id}", headers=self.auth(self.student)
        )
        self.assert_success(response, message="已忘记")
        self.assertEqual(self.db.query(UserMemory).filter_by(id=row.id).count(), 0)

    def test_forgetting_a_missing_memory_is_a_404_business_code(self) -> None:
        response = self.client.delete(
            f"{MEMORY}/99999", headers=self.auth(self.student)
        )
        self.assert_business_error(response, "记忆不存在", code=404)

    def test_someone_elses_memory_looks_exactly_like_a_missing_one(self) -> None:
        """不给「这条记忆存在，但不是你的」这种可探测的差别。"""
        row = self.make_memory(self.other, "别人的记忆")
        response = self.client.delete(
            f"{MEMORY}/{row.id}", headers=self.auth(self.student)
        )
        self.assert_business_error(response, "记忆不存在", code=404)
        self.assertEqual(self.db.query(UserMemory).filter_by(id=row.id).count(), 1)

    def test_memory_id_must_be_positive(self) -> None:
        self.assert_validation_error(
            self.client.delete(f"{MEMORY}/0", headers=self.auth(self.student))
        )


class ConversationCases(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.student = self.make_user("zhangsan")
        self.other = self.make_user("lisi")

    def _list(self, query: str = "", user=None):
        return self.client.get(
            CONVERSATIONS + query, headers=self.auth(user or self.student)
        )

    def _detail(self, cid: str, user=None):
        return self.client.get(
            f"{CONVERSATIONS}/{cid}", headers=self.auth(user or self.student)
        )

    def _delete(self, cid: str, user=None):
        return self.client.delete(
            f"{CONVERSATIONS}/{cid}", headers=self.auth(user or self.student)
        )

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------

    def test_only_my_conversations_are_listed(self) -> None:
        self.make_conversation(self.student, "mine-1")
        self.make_conversation(self.other, "theirs-1")
        data = self.assert_success(self._list())
        self.assertEqual([item["conversation_id"] for item in data], ["mine-1"])

    def test_the_list_is_newest_first(self) -> None:
        first = self.make_conversation(self.student, "older")
        second = self.make_conversation(self.student, "newer")
        # last_time 是 NOT NULL，只能往前挪，不能置空
        first.last_time = datetime.now() - timedelta(days=1)
        self.db.commit()
        data = self.assert_success(self._list())
        self.assertEqual(
            [item["conversation_id"] for item in data],
            [second.conversation_id, first.conversation_id],
        )

    def test_the_list_carries_no_message_bodies(self) -> None:
        """侧栏只需要「认出这是哪次对话」，正文不在列表里回传。"""
        self.make_conversation(self.student, "c1")
        self.make_message(self.student, "c1", "user", "帮我约实验室")
        item = self.assert_success(self._list())[0]
        self.assertNotIn("messages", item)
        self.assertEqual(item["title"], "帮我约明天的计算机实验室")

    def test_the_limit_is_bounded(self) -> None:
        for index in range(3):
            self.make_conversation(self.student, f"c{index}")
        self.assertEqual(len(self.assert_success(self._list("?limit=2"))), 2)
        self.assertEqual(len(self.assert_success(self._list("?limit=1"))), 1)
        self.assertEqual(len(self.assert_success(self._list("?limit=200"))), 3)
        for bad in ("0", "201", "abc"):
            with self.subTest(limit=bad):
                self.assert_validation_error(self._list(f"?limit={bad}"))

    def test_the_list_requires_authentication(self) -> None:
        self.assert_unauthorized(self.client.get(CONVERSATIONS))

    # ------------------------------------------------------------------
    # 详情
    # ------------------------------------------------------------------

    def test_a_conversation_can_be_reopened_with_all_its_messages(self) -> None:
        self.make_conversation(
            self.student, "c1", title="帮我约实验室", message_count=2
        )
        self.make_message(self.student, "c1", "user", "帮我约明天的计算机实验室")
        self.make_message(self.student, "c1", "assistant", "好的，已为您预约上午 9 点")

        data = self.assert_success(self._detail("c1"))
        self.assertEqual(data["conversation_id"], "c1")
        self.assertEqual(data["title"], "帮我约实验室")
        self.assertEqual(data["message_count"], 2)
        self.assertIsNotNone(data["last_time"])
        self.assertEqual(
            [(item["role"], item["content"]) for item in data["messages"]],
            [
                ("user", "帮我约明天的计算机实验室"),
                ("assistant", "好的，已为您预约上午 9 点"),
            ],
        )

    def test_a_missing_conversation_is_a_404_business_code(self) -> None:
        response = self._detail("never-existed")
        self.assert_business_error(response, "该会话不存在或已超过保留期", code=404)

    def test_another_users_conversation_looks_exactly_like_a_missing_one(self) -> None:
        """措辞统一成「不存在或已超过保留期」，不泄露这个 id 到底存不存在。"""
        self.make_conversation(self.other, "c1")
        self.make_message(self.other, "c1", "user", "别人的隐私")
        response = self._detail("c1")
        self.assert_business_error(response, "该会话不存在或已超过保留期", code=404)
        self.assertNotIn("别人的隐私", response.text)

    def test_conversation_id_length_is_capped_on_detail(self) -> None:
        self.assert_validation_error(self._detail("x" * 65))

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    def test_deleting_a_conversation_removes_its_messages_too(self) -> None:
        self.make_conversation(self.student, "c1")
        self.make_message(self.student, "c1", "user", "第一句")
        self.make_message(self.student, "c1", "assistant", "第二句")

        body = self.assert_success(self._delete("c1"), message="已删除")
        self.assertEqual(body, {"deleted": True})
        self.assertEqual(
            self.db.query(AiConversation).filter_by(conversation_id="c1").count(), 0
        )
        self.assertEqual(
            self.db.query(AiMessage).filter_by(conversation_id="c1").count(), 0
        )

    def test_deleting_twice_is_idempotent_not_an_error(self) -> None:
        self.make_conversation(self.student, "c1")
        self.assertEqual(self.assert_success(self._delete("c1")), {"deleted": True})
        second = self.assert_success(self._delete("c1"), message="已删除")
        self.assertEqual(second, {"deleted": False})

    def test_i_cannot_delete_someone_elses_conversation(self) -> None:
        """猜中别人的 conversation_id 也不能删除其消息。"""
        self.make_conversation(self.other, "c1")
        self.make_message(self.other, "c1", "user", "别人的记录")

        body = self.assert_success(self._delete("c1"))
        self.assertEqual(body, {"deleted": False})

        # 会话行按 user_id 过滤，别人的留下了
        self.assertEqual(
            self.db.query(AiConversation).filter_by(conversation_id="c1").count(), 1
        )
        self.assertEqual(
            self.db.query(AiMessage).filter_by(conversation_id="c1").count(), 1
        )
        self.assertEqual(self.db.query(AiMessage).count(), 1)

    def test_deleting_a_conversation_never_touches_other_tables_of_other_users(
        self,
    ) -> None:
        """删除只按 conversation_id + user_id 走，别把别人的轨迹顺手清了。"""
        self.make_conversation(self.other, "c-other")
        their_trace = self.make_trace(self.other, "c-other")
        their_call = self.make_tool_execution(self.other, "c-other")
        self.make_conversation(self.student, "c-mine")
        self.assert_success(self._delete("c-mine"))

        self.assertEqual(
            self.db.query(AgentTrace).filter_by(id=their_trace.id).count(), 1
        )
        self.assertEqual(
            self.db.query(AgentToolExecution).filter_by(id=their_call.id).count(), 1
        )

    def test_deleting_requires_authentication(self) -> None:
        self.make_conversation(self.student, "c1")
        self.assert_unauthorized(self.client.delete(f"{CONVERSATIONS}/c1"))
        self.assertEqual(
            self.db.query(AiConversation).filter_by(conversation_id="c1").count(), 1
        )


if __name__ == "__main__":
    unittest.main()
