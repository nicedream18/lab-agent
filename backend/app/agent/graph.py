"""LangGraph 工作流编排 —— 整个 Agent 的控制流都在这一个文件里。

    START → analyze → plan → route ⇄ execute → reflect → respond → END
                              ↑                   │
                              └───── replan ──────┘

与改造前的区别：
  改造前是 ReAct 循环（agent ⇄ tools），控制流完全由 LLM 的 tool_call 驱动；
  现在是显式工作流，每个环节的职责、失败处理、循环上限都由代码确定。
  LLM 只在「理解、规划、路由、反思、措辞」这五个点上被调用，
  它无法绕过 reflector 直接结束，也无法跳过授权闸门直接写库。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import AsyncIterator

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.agent import memory, planner, tools
from app.agent.state import (
    MAX_REPLAN_ROUNDS,
    AgentContext,
    AgentState,
    PlanStep,
)
from app.agent.tracer import trace_node
from app.models.user import User

logger = logging.getLogger(__name__)

# 出现失败就跳过剩余步骤、直接进反思的工具。
# 这几步失败了后面做了也没意义：实验室都查不到，创建预约必然失败。
CRITICAL_TOOLS = {"query_lab_availability", "check_user_permission", "create_reservation"}

# 图的最大超步数。按最坏情况估算：
# 3 轮（初次 + 2 次重规划）×（2 编排 + 6×2 执行）≈ 42，留一倍余量。
RECURSION_LIMIT = 60


def _bind_writer(ctx: AgentContext) -> None:
    """每个节点开头调用：把 LangGraph 的流式写入器挂到 ctx 上。

    get_stream_writer() 依赖 contextvars，必须**在节点内部**调用才拿得到。
    """
    try:
        ctx.writer = get_stream_writer()
    except Exception:  # noqa: BLE001
        ctx.writer = None


# ---------------------------------------------------------------------------
# 节点实现
# ---------------------------------------------------------------------------


def node_analyze(ctx: AgentContext):
    def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        with trace_node(ctx, "analyze", input_text=state.get("user_query") or "") as box:
            result = planner.analyze(ctx, state)
            box["output"] = (
                f"意图={result['intent']}；"
                f"槽位={result['slots']}；"
                f"缺失={result['missing_slots']}；"
                f"授权={result['authorized']}"
            )
            ctx.emit(
                {
                    "type": "analysis",
                    "intent": result["intent"],
                    "slots": result["slots"],
                    "missing_slots": result["missing_slots"],
                    "authorized": result["authorized"],
                    "detail": result.get("reason") or box["output"],
                }
            )
            return result

    return run


def node_plan(ctx: AgentContext):
    def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        round_no = state.get("replan_round") or 0

        extra = ""
        if round_no > 0:
            # 重新规划时必须把失败原因喂回去，否则模型会生成一模一样的计划，
            # 白白烧掉一轮重试额度。
            extra = (
                f"\n## ⚠️ 这是第 {round_no} 次重新规划\n"
                f"上一次计划执行后，反思结论是：{state.get('reflection') or ''}\n"
                "请针对这个问题调整计划，不要重复上次已经失败的做法。\n"
            )
            if state.get("read_only"):
                # 本轮是只读探索：换时间属于改变用户的诉求，必须由用户点头，
                # 所以只查不写。
                extra += (
                    "【本轮限制】本轮只允许查询类工具。请调用 find_available_slots "
                    "找出该实验室当天的替代空闲时段，把结果整理出来给用户挑选；"
                    "**禁止**调用 create_reservation，系统也会拦截该工具。\n"
                )
            else:
                extra += (
                    "如果是时段冲突，可以改用 find_available_slots 找出替代时段。\n"
                )

        with trace_node(ctx, "plan", input_text=state.get("user_query") or "") as box:
            plan = planner.make_plan(ctx, state, extra=extra)
            box["output"] = (
                f"生成 {len(plan)} 步计划：" + " → ".join(item["tool"] for item in plan)
                if plan
                else "无需调用工具，直接回答"
            )
            ctx.emit(
                {
                    "type": "plan",
                    "round": round_no,
                    "steps": [
                        {
                            "id": item["id"],
                            "tool": item["tool"],
                            "label": tools.tool_label(item["tool"]),
                            "reason": item.get("reason") or "",
                        }
                        for item in plan
                    ],
                }
            )
            return {"plan": plan, "cursor": 0, "halt": False}

    return run


def node_route(ctx: AgentContext):
    def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        plan = [dict(item) for item in (state.get("plan") or [])]
        cursor = state.get("cursor") or 0
        if cursor >= len(plan):
            return {"plan": plan}

        step: PlanStep = plan[cursor]
        tool_name = step["tool"]

        with trace_node(
            ctx, "route", input_text=f"第{step['id']}步 {tool_name}"
        ) as box:
            args = dict(step.get("args") or {})
            if planner.args_complete(step):
                # 计划里参数就齐了 → 不浪费一次模型调用
                reason = step.get("reason") or ""
                # 但仍然过一道槽位回填：计划可能把 lab_name 写成了 null，
                # 参数“数量齐”不等于“值非空”。
                args = planner.backfill_args(args, state, tool_name)
                box["output"] = f"参数已完备，直接执行：{args}"
            else:
                args, reason = planner.resolve_args(ctx, state, step)
                box["output"] = f"补全参数：{args}｜依据：{reason}"

            step["args"] = args
            step["status"] = "running"
            plan[cursor] = step

            ctx.emit(
                {
                    "type": "step",
                    "id": step["id"],
                    "tool": tool_name,
                    "label": tools.tool_label(tool_name),
                    "status": "running",
                    "args": args,
                    "reason": reason,
                }
            )
            return {"plan": plan}

    return run


def node_execute(ctx: AgentContext):
    def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        plan = [dict(item) for item in (state.get("plan") or [])]
        cursor = state.get("cursor") or 0
        if cursor >= len(plan):
            return {}

        step: PlanStep = plan[cursor]
        tool_name = step["tool"]
        args = step.get("args") or {}

        results = dict(state.get("tool_results") or {})
        observations = list(state.get("observations") or [])

        with trace_node(
            ctx, "execute", input_text=f"{tool_name}({args})"
        ) as box:
            result = tools.run_tool(ctx, tool_name, args)
            results[tool_name] = result

            summary = tools.summarize(tool_name, result)
            observations.append(summary)
            box["output"] = summary
            if result.get("ok") is False:
                box["status"] = "failed"
                step["status"] = "failed"
            else:
                step["status"] = "done"

            ctx.emit(
                {
                    "type": "step",
                    "id": step["id"],
                    "tool": tool_name,
                    "label": tools.tool_label(tool_name),
                    "status": step["status"],
                    "detail": summary,
                    "result": result,
                }
            )

        plan[cursor] = step
        cursor += 1

        # 预约成功后，把偏好沉淀进长期记忆
        if tool_name == "create_reservation" and result.get("ok"):
            try:
                learned = memory.learn_from_reservation(
                    ctx.db,
                    ctx.user,
                    lab_name=result.get("lab_name") or "",
                    start_time=result.get("start_time") or "",
                )
                if learned:
                    logger.info("已更新用户长期偏好：%s", learned)
            except Exception:  # noqa: BLE001
                # 学偏好失败不能影响主流程
                logger.exception("写入长期记忆失败")

        # 致命工具失败 → 跳过剩余步骤，直接进反思
        halt = bool(result.get("ok") is False and tool_name in CRITICAL_TOOLS)

        return {
            "plan": plan,
            "cursor": cursor,
            "tool_results": results,
            "observations": observations,
            "halt": halt,
        }

    return run


def node_reflect(ctx: AgentContext):
    def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        round_no = state.get("replan_round") or 0

        with trace_node(
            ctx, "reflect", input_text=planner.render_execution_log(state)
        ) as box:
            result = planner.reflect(ctx, state)
            verdict = result["verdict"]

            # 业务规则险：模型说“结束”，但业务上明显还有救（如目标时段被占），
            # 就强制转成重规划。控制流宁可由代码说了算，也不由模型随机性说了算。
            forced = planner.should_replan(state)
            if verdict != "replan" and forced:
                logger.info("覆盖模型判定 replan：%s", forced)
                verdict = "replan"
                result["reflection"] = f"{result['reflection']}\n【系统判定】{forced}"

            # 只读轮次跑完必须收尾：该查的都查到了，剩下的只是“把结果讲清楚”，
            # 再重规划只会把同一个查询重复一遍、白烧模型额度。
            if state.get("read_only") and verdict == "replan":
                logger.info("只读探索轮已结束，强制收尾")
                verdict = "finish"
                result["reflection"] = (
                    f"{result['reflection']}（系统：替代时段已查回，转为给出结论）"
                )

            # 重试次数用尽后强制收尾：不能让 Agent 无限空转
            if verdict == "replan" and round_no >= MAX_REPLAN_ROUNDS:
                logger.info("重新规划次数已达上限 %s，强制结束", MAX_REPLAN_ROUNDS)
                verdict = "finish"
                result["reflection"] = (
                    f"{result['reflection']}（已达到最大重规划次数，"
                    "改为直接给出当前结论）"
                )

            box["output"] = f"判定={verdict}；{result['reflection']}"
            ctx.emit(
                {
                    "type": "reflection",
                    "verdict": verdict,
                    "text": result["reflection"],
                    "round": round_no,
                }
            )

            update: dict = {
                "verdict": verdict,
                "reflection": result["reflection"],
            }
            if verdict == "replan":
                update["replan_round"] = round_no + 1
                # 因时段冲突触发的重规划：本轮定为只读，
                # 只查替代时段、不替用户自作主张下单
                if forced:
                    update["read_only"] = True
            return update

    return run


def node_respond(ctx: AgentContext):
    async def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        with trace_node(ctx, "respond", input_text=state.get("reflection") or "") as box:
            def emit_token(text: str) -> None:
                ctx.emit({"type": "token", "content": text})

            text = await planner.stream_response_safe(ctx, state, emit_token)
            box["output"] = text
            return {"final_response": text}

    return run


# ---------------------------------------------------------------------------
# 条件边
# ---------------------------------------------------------------------------


def _after_plan(state: AgentState) -> str:
    """计划为空说明不需要调工具，直接去回复。"""
    return "route" if (state.get("plan") or []) else "respond"


def _after_route(state: AgentState) -> str:
    cursor = state.get("cursor") or 0
    return "execute" if cursor < len(state.get("plan") or []) else "reflect"


def _after_execute(state: AgentState) -> str:
    """还有剩余步骤就继续，否则（或遇到致命失败）进入反思。"""
    if state.get("halt"):
        return "reflect"
    cursor = state.get("cursor") or 0
    return "route" if cursor < len(state.get("plan") or []) else "reflect"


def _after_reflect(state: AgentState) -> str:
    return "plan" if state.get("verdict") == "replan" else "respond"


# ---------------------------------------------------------------------------
# 图构建与执行
# ---------------------------------------------------------------------------


def build_graph(ctx: AgentContext):
    graph = StateGraph(AgentState)

    graph.add_node("analyze", node_analyze(ctx))
    graph.add_node("plan", node_plan(ctx))
    graph.add_node("route", node_route(ctx))
    graph.add_node("execute", node_execute(ctx))
    graph.add_node("reflect", node_reflect(ctx))
    graph.add_node("respond", node_respond(ctx))

    graph.add_edge(START, "analyze")
    graph.add_edge("analyze", "plan")
    graph.add_conditional_edges(
        "plan", _after_plan, {"route": "route", "respond": "respond"}
    )
    graph.add_conditional_edges(
        "route", _after_route, {"execute": "execute", "reflect": "reflect"}
    )
    graph.add_conditional_edges(
        "execute", _after_execute, {"route": "route", "reflect": "reflect"}
    )
    graph.add_conditional_edges(
        "reflect", _after_reflect, {"plan": "plan", "respond": "respond"}
    )
    graph.add_edge("respond", END)

    return graph.compile()


def _initial_state(
    ctx: AgentContext, query: str, history: list[dict]
) -> AgentState:
    """构造初始状态。

    memory_context 在这里就读出来，而不是在 respond 节点里读：
    1. 只读一次库，避免每个节点都查一遍；
    2. Planner 也需要看到偏好（比如「用户习惯约下午」会影响计划）。
    """
    try:
        memory_context = memory.load_memory_context(ctx.db, ctx.user.id)
    except Exception:  # noqa: BLE001
        logger.exception("读取长期记忆失败")
        memory_context = "（记忆读取失败）"

    return AgentState(
        user_id=ctx.user.id,
        conversation_id=ctx.conversation_id,
        today=datetime.now().strftime("%Y-%m-%d"),
        user_query=query,
        history=history,
        memory_context=memory_context,
        plan=[],
        cursor=0,
        replan_round=0,
        tool_results={},
        observations=[],
        halt=False,
        final_response="",
    )


def prepare(
    db: Session, user: User, conversation_id: str, query: str
) -> tuple[AgentContext, AgentState, dict]:
    """非流式入口：跑完整个工作流，返回 (ctx, 终态, 图)。"""
    ctx = AgentContext(db=db, user=user, conversation_id=conversation_id)
    history = memory.get_history(conversation_id)
    state = _initial_state(ctx, query, history)
    return ctx, state, build_graph(ctx)


class WorkflowRunner:
    """一次问答的执行器。流式和非流式共用同一张图。"""

    def __init__(self, db: Session, user: User, conversation_id: str, query: str):
        self.ctx = AgentContext(db=db, user=user, conversation_id=conversation_id)
        self.query = query
        self.history = memory.get_history(conversation_id)
        self.state = _initial_state(self.ctx, query, self.history)
        self.graph = build_graph(self.ctx)

    # -- 流式 ------------------------------------------------------------
    async def astream(self) -> AsyncIterator[dict]:
        """跑工作流并把每个节点产生的事件实时吐出去。

        用 stream_mode="custom"：节点内部通过 get_stream_writer() 主动推送事件，
        比解析 updates 更灵活（可以把 token 粒度的事件也带出来）。
        """
        final_state: dict = dict(self.state)
        try:
            async for mode, payload in self.graph.astream(
                self.state,
                stream_mode=["custom", "values"],
                config={"recursion_limit": RECURSION_LIMIT},
            ):
                if mode == "custom":
                    yield payload
                else:
                    # values 模式每次给出完整状态，留最后一份即可拿到 final_response
                    final_state = payload
        except Exception:  # noqa: BLE001
            logger.exception("Agent 工作流执行失败")
            yield {"type": "error", "message": "Agent 执行失败，请稍后重试"}
            return

        answer = (final_state or {}).get("final_response") or ""
        memory.append_turn(self.ctx.conversation_id, self.query, answer)
        yield {"type": "done", "answer": answer}

    # -- 非流式 ----------------------------------------------------------
    async def ainvoke(self) -> str:
        try:
            result = await self.graph.ainvoke(
                self.state, config={"recursion_limit": RECURSION_LIMIT}
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent 工作流执行失败")
            raise

        answer = (result or {}).get("final_response") or ""
        memory.append_turn(self.ctx.conversation_id, self.query, answer)
        return answer
