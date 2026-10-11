"""离线执行图回归：脚本化模型和工具，真实运行 LangGraph 控制流。"""

import json
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage

from evals.offline_env import configure_offline

configure_offline()

from app.agent import graph, planner, tools
from app.agent.state import AgentContext


@contextmanager
def trace_without_database(*_args, **_kwargs):
    yield {"output": "", "status": "success"}


def call(name, index=1, **args):
    return AIMessage(
        content="", tool_calls=[{"id": f"tool-{index}", "name": name, "args": args}]
    )


class WorkflowCases(unittest.IsolatedAsyncioTestCase):
    async def run_case(
        self,
        query,
        model_replies,
        tool_responses=None,
        *,
        plan=None,
        model_authorized=False,
    ):
        analysis = json.dumps(
            {
                "slots": {"lab_name": "计算机实验室"},
                "missing_slots": [],
                "authorized": model_authorized,
                "plan_needed": plan is not None,
            }
        )
        tool_responses = tool_responses or {}
        with (
            patch.object(planner, "_ask", return_value=analysis),
            patch.object(
                planner, "agent_step", new_callable=AsyncMock, side_effect=model_replies
            ),
            patch.object(
                planner,
                "make_plan",
                return_value={"plan": plan, "plan_revision": 0, "step_results": []},
            ),
            patch.object(graph, "trace_node", trace_without_database),
            patch.object(graph, "write_tool_execution"),
            patch.object(graph.memory, "load_memory_context", return_value=""),
            patch.object(graph.memory, "append_turn"),
            patch.object(graph.memory, "learn_from_reservation"),
            patch.object(
                tools,
                "run_tool",
                side_effect=lambda _ctx, name, _args: tool_responses[name],
            ) as run_tool,
        ):
            runner = graph.WorkflowRunner(
                object(), SimpleNamespace(id=7), "offline-test", query, history=[]
            )
            answer = await runner.ainvoke()
            return answer, run_tool

    async def test_read_query(self):
        answer, run_tool = await self.run_case(
            "有哪些实验室开放？",
            [call("list_open_labs"), AIMessage(content="计算机实验室开放。")],
            {"list_open_labs": {"ok": True, "labs": ["计算机实验室"]}},
            model_authorized=True,
        )
        self.assertIn("计算机实验室", answer)
        self.assertEqual(run_tool.call_count, 1)

    async def test_multistep_plan(self):
        plan = [
            {
                "step": 1,
                "goal": "查找设备",
                "hint_tools": ["list_lab_equipments"],
                "depends_on": [],
            },
            {
                "step": 2,
                "goal": "查询时段",
                "hint_tools": ["query_lab_availability"],
                "depends_on": [1],
            },
        ]
        answer, run_tool = await self.run_case(
            "先查哪间实验室有显卡，再看明天下午是否有空",
            [
                call("list_lab_equipments"),
                AIMessage(content="已找到实验室"),
                call("query_lab_availability", 2),
                AIMessage(content="计算机实验室明天下午有空。"),
            ],
            {
                "list_lab_equipments": {"ok": True, "lab_name": "计算机实验室"},
                "query_lab_availability": {"ok": True, "available": True},
            },
            plan=plan,
        )
        self.assertIn("有空", answer)
        self.assertEqual(
            [c.args[1] for c in run_tool.call_args_list],
            ["list_lab_equipments", "query_lab_availability"],
        )

    async def test_knowledge_base_answer(self):
        answer, run_tool = await self.run_case(
            "实验室能带食物吗？",
            [
                call("search_lab_docs", query="能带食物吗"),
                AIMessage(content="安全规范不允许带食物。"),
            ],
            {"search_lab_docs": {"ok": True, "content": "禁止携带食物"}},
        )
        self.assertIn("不允许", answer)
        run_tool.assert_called_once()

    async def test_ambiguous_cancel_does_not_retry_write(self):
        answer, run_tool = await self.run_case(
            "帮我取消这个预约",
            [
                call("cancel_reservation"),
                AIMessage(content="找到两条预约，请提供编号。"),
            ],
            {"cancel_reservation": {"ok": False, "error": "匹配到两条待审核预约"}},
        )
        self.assertIn("请提供编号", answer)
        self.assertEqual(run_tool.call_count, 1)

    async def test_out_of_scope_write_is_blocked(self):
        answer, run_tool = await self.run_case(
            "帮我预约计算机实验室",
            [call("cancel_reservation"), AIMessage(content="没有执行取消。")],
            model_authorized=True,
        )
        self.assertIn("没有执行取消", answer)
        run_tool.assert_not_called()

    async def test_runtime_gate_rejects_forged_bound_write(self):
        # 即使上游状态错误地声称曾绑定取消工具，执行层仍按本轮具体许可拒绝。
        ctx = AgentContext(
            db=object(), user=SimpleNamespace(id=7), conversation_id="test"
        )
        state = {
            "pending_calls": [
                {"id": "tool-1", "step": 1, "name": "cancel_reservation", "args": {}}
            ],
            "bound_tools": ["cancel_reservation"],
            "allowed_write_tools": ["create_reservation"],
            "tool_results": [],
            "plan_cursor": 0,
        }
        with (
            patch.object(graph, "trace_node", trace_without_database),
            patch.object(graph, "write_tool_execution"),
            patch.object(tools, "run_tool") as run_tool,
        ):
            result = graph.node_execute(ctx)(state)
        self.assertFalse(result["tool_results"][0]["ok"])
        run_tool.assert_not_called()

    async def test_plan_cannot_hint_other_write_tool(self):
        plan = planner._normalize_plan(
            [
                {
                    "goal": "取消并预约",
                    "hint_tools": ["cancel_reservation", "create_reservation"],
                }
            ],
            {"create_reservation"},
        )
        self.assertEqual(plan[0]["hint_tools"], ["create_reservation"])

    async def test_model_false_permission_does_not_block_explicit_booking(self):
        answer, run_tool = await self.run_case(
            "帮我预约计算机实验室",
            [call("create_reservation"), AIMessage(content="预约已提交。")],
            {"create_reservation": {"ok": True, "reservation_id": 123}},
            model_authorized=False,
        )
        self.assertIn("预约已提交", answer)
        run_tool.assert_called_once()

    async def test_model_failure_stops_before_tools(self):
        with (
            patch.object(planner, "_ask", side_effect=RuntimeError("offline model")),
            patch.object(graph, "trace_node", trace_without_database),
            patch.object(graph.memory, "load_memory_context", return_value=""),
            patch.object(graph.memory, "append_turn"),
            patch.object(tools, "run_tool") as run_tool,
        ):
            answer = await graph.WorkflowRunner(
                object(), SimpleNamespace(id=7), "offline-test", "帮我预约实验室"
            ).ainvoke()
        self.assertTrue(answer)
        run_tool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
