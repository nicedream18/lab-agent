"""LangGraph 工作流编排 —— 整个 Agent 的控制流都在这一个文件里。

    START → analyze → agent ⇄ execute → respond → END          （快路径）

    START → analyze → plan → agent ⇄ execute → advance → ⇢    （规划路径）
                                   ↓
                            _after_agent → replan → ⇢
                                         └→ respond → END

规划路径是**条件触发**的：analyze 在解析需求时顺带判定 plan_needed，
只有 True 才走 plan 分支。单查询 / 闲聊走的就是上面那条与 v2 一模一样的
快路径 —— 拓扑和行为都没有变化。

每个环节做什么：
  * analyze —— 把用户原话解析成「槽位 / 还缺什么 / 能不能写库 / 要不要规划」。
              只问模型一次，用结构化输出，因为它的产出是给**系统**看的：
              authorized 决定写库工具要不要交给模型，plan_needed 决定要不要
              先拆计划。
  * plan    —— 把请求拆成有序步骤（仅在 plan_needed=True 时到达）。
              每一步带「目标 + 建议工具」，但**不规定**具体参数。
  * agent   —— 自主决策。把用户原话、已知槽位、当前步骤上下文、工具清单
              一起交给模型，**由模型自己决定调哪个工具、带什么参数**。
              它返回的 tool_calls 就是调用指令本身。
  * execute —— 执行模型要的那几个工具，把结果作为 ToolMessage 交回去，
              然后回到 agent 看下一步。循环由 tool_round / step_round 收口。
  * advance —— 纯管道：给本步记账、把我的游标推到下一步，然后回 agent。
  * replan  —— 本步没做成且还有备用工具时，让模型重排**剩余**步骤。
              由 _replan_needed 那道确定性闸门把关（见那里）。
  * respond —— 把最后的自然语言结论吐给用户。

与上一版（v2）的区别：
  v2 只有一个由模型 tool_calls 驱动的 ReAct 循环，所有请求都走同一条路。
  v3 在其前面加了一层**按需**的规划：简单请求一点额外开销都不付，
  复杂请求则多一份「先拆解、逐步执行、走偏了再调整」的能力。

  规划层没有引入任何「关键词判断」——要不要规划完全由模型在 analyze 阶段
  自己给出 plan_needed。关键词正则在全系统里只用于两件事：写库授权的
  安全闸门，以及日期的确定性核对。它们都不决定「走哪条路」。

安全与边界仍是三个硬手段（与 v2 一致，未因规划层而放宽）：
    1. 没拿到写库授权时，写库工具**根本不在 bind_tools 的清单里**；
       规划层收窄工具时也只能在这个清单的**子集**里收窄，绕不过去。
    2. execute 节点逐条拦截未授权的写库调用，作为第二道闸门。
    3. 循环上限由 MAX_TOOL_ROUNDS（全局）与 STEP_TOOL_ROUNDS（每步）收口，
       最后一轮把 tool_choice 置为 "none"，逼模型基于已有观察给结论。
    另加一道 v3 专属的幂等闸门：重规划后重复提交同一次写库会被跳过。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, cast

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.agent import memory, planner, tools
from app.agent.observability import request_tracing
from app.agent.state import (
    MAX_REPLAN_ROUNDS,
    MAX_TOOL_ROUNDS,
    STEP_TOOL_ROUNDS,
    WRITE_TOOLS,
    AgentContext,
    AgentState,
)
from app.agent.tracer import trace_node, write_tool_execution
from app.common.async_io import run_sync
from app.models.reservation import Reservation
from app.models.user import User
from app.services import cancellation_draft_service as cancellations
from app.services import reservation_draft_service as drafts

logger = logging.getLogger(__name__)

# 未授权的写库调用被拦下时，交给模型的那句话。
# 它会以 ToolMessage 的形式回到模型面前 —— 模型下一轮看到的是「你没被授权」
# 这个事实，而不是一个语焉不详的失败，这样它才会转去跟用户确认，
# 而不是重试同一次调用。
UNAUTHORIZED_WRITE = (
    "当前未获得写库授权，写库工具本轮不可用。"
    "如果用户确实想创建或取消预约，请先用一句话向他确认，不要重试本次调用。"
)

# 图的最大超步数。
#
# v2 的估算：analyze + (agent + execute) × MAX_TOOL_ROUNDS + respond ≈ 16。
# v3 加入规划层后，最坏情形变成：
#   analyze + plan + replan×2 + respond            = 5
#   (agent + execute) × MAX_TOOL_ROUNDS            = 12
#   每步收尾的「无工具」agent 轮（≤ MAX_PLAN_STEPS + MAX_REPLAN_ROUNDS = 7） = 7
#   advance × ≤7                                    = 7
#                                                   ------
#                                                   ≈ 31
# 取 60 留了近一倍余量。这个数字不是「随便放大一点」——它必须同时大于
# 最坏路径，又不能大到让真正的死循环（比如某条边写错了）跑很久才发现。
RECURSION_LIMIT = 60


def _bind_writer(ctx: AgentContext) -> None:
    """每个节点开头调用：把 LangGraph 的流式写入器挂到 ctx 上。

    get_stream_writer() 依赖 contextvars，必须**在节点内部**调用才拿得到。
    """
    try:
        ctx.writer = get_stream_writer()
    except Exception:  # noqa: BLE001
        ctx.writer = None


_CONFIRM_BOOKING = re.compile(
    r"^\s*(?:确认|确定|提交)(?:这条|这个|该)?(?:预约|预订|预定|订单)[。！!\s]*$"
)
_BOOKING_FOLLOWUP = re.compile(
    r"^\s*(?:就按|按|就)(?:这个|这条|刚才的)?(?:时段|时间)?(?:预约|预订|预定|约)[。！!\s]*$"
)
_SLOT_REPLY = re.compile(
    r"^(?!.*(?:吗|能否|可不可以|怎么|取消|不约|不要|别|先查|查询|帮我查|看看|查一下))(?=.*(?:今天|明天|后天|\d{1,4}月|\d{1,2}[点时:]|上午|下午|晚上))[\s\S]{1,80}$"
)
_EXPLICIT_RANGE = re.compile(
    r"(?:[零一二三四五六七八九十两]{1,3}\s*[点时]|\d{1,2}\s*[点时]|\d{1,2}[:：]\d{2})"
    r"\s*(?:到|至|[-–~])(?:今天|明天|后天|上午|下午|晚上|\s){0,8}"
    r"(?:[零一二三四五六七八九十两]{1,3}\s*[点时]|\d{1,2}\s*[点时]|\d{1,2}[:：]\d{2})"
)


def _cancel_selection(query: str, ids: list[int], db: Session, user_id: int, model_intent: str = "") -> list[int]:
    """只解析当前用户对已保存候选的简短选择，绝不从历史文字猜编号。"""
    query = query.strip()
    if re.search(r"吗|么|[？?]|怎么|如何|能否|能不能|可不可以|不要.*取消|别取消|先别|先不要|先不|暂不|不取消|算了", query):
        return []
    if re.fullmatch(r"(?:都|全部|全都|两个|这两条|这些)(?:取消|退掉|不去了)[。！!\s]*", query) or (
        model_intent == "cancel_all"
        and re.fullmatch(
            r"(?:那|把)?(?:这两条|两条|这两个|都|全部|全都|它们)(?:都)?"
            r"(?:不要了|不去了|退了)[。！!\s]*", query
        )
    ):
        return ids
    id_phrase = re.search(r"预约(?:号|编号)?\s*(\d+(?:\s*(?:和|、|,|，)\s*\d+)*)", query)
    mentioned = [int(value) for value in re.findall(r"\d+", id_phrase.group(1))] if id_phrase else []
    if not mentioned and re.fullmatch(r"\s*\d+(?:\s*(?:和|、|,|，)\s*\d+)*\s*", query):
        mentioned = [int(value) for value in re.findall(r"\d+", query)]
    if mentioned and all(item in ids for item in mentioned):
        return list(dict.fromkeys(mentioned))
    times = re.findall(r"(?<!\d)(\d{1,2}):?(\d{2})\s*(?:到|[-–~])\s*(\d{1,2}):?(\d{2})", query)
    if times:
        rows = db.query(Reservation).filter(
            Reservation.user_id == user_id, Reservation.id.in_(ids)
        ).all()
        selected = [
            row.id for row in rows
            if any(row.start_time == f"{int(a):02d}:{b}" and row.end_time == f"{int(c):02d}:{d}" for a,b,c,d in times)
        ]
        return selected if len(selected) == len(times) else []
    spoken = re.search(
        r"(上午|下午|晚上)?\s*([零一二两三四五六七八九十\d]{1,3})\s*[点时]"
        r"\s*(?:到|至|[-–~])\s*(上午|下午|晚上)?\s*"
        r"([零一二两三四五六七八九十\d]{1,3})\s*[点时]", query
    )
    if spoken:
        numerals = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

        def hour(value: str, period: str | None) -> int | None:
            if value.isdigit():
                number = int(value)
            elif value in numerals:
                number = numerals[value]
            elif value.startswith("十") and len(value) == 2 and value[1] in numerals:
                number = 10 + numerals[value[1]]
            else:
                return None
            if period in ("下午", "晚上") and number < 12:
                number += 12
            return number if 0 <= number <= 23 else None

        period = spoken.group(1)
        start = hour(spoken.group(2), period)
        end = hour(spoken.group(4), spoken.group(3) or period)
        if start is not None and end is not None:
            rows = db.query(Reservation).filter(
                Reservation.user_id == user_id, Reservation.id.in_(ids)
            ).all()
            selected = [
                row.id for row in rows
                if row.start_time == f"{start:02d}:00" and row.end_time == f"{end:02d}:00"
            ]
            return selected if len(selected) == 1 else []
    return []


def _prepare_cancel_draft(ctx: AgentContext, state: AgentState, result: dict) -> dict:
    query = state.get("user_query") or ""
    draft = cancellations.load(ctx.db, ctx.user.id, ctx.conversation_id)
    selected = _cancel_selection(query, draft.reservation_ids, ctx.db, ctx.user.id, result.get("write_followup") or "") if draft else []
    if not selected and planner.explicit_write_request(query) == "cancel_reservation" and not re.search(r"预约(?:号|编号)?\s*\d+", query):
        date = _cancel_date(state)
        rows = ctx.db.query(Reservation).filter_by(
            user_id=ctx.user.id, date=date, status=0
        ).order_by(Reservation.id).all() if date else []
        if 1 < len(rows) <= 20:
            result.update(authorized=False, allowed_write_tools=[], cancel_pending=True,
                          cancel_candidate_details=[{
                              "reservation_id": row.id,
                              "lab_name": row.lab.name if row.lab else "实验室",
                              "equipment_name": row.equipment.name if row.equipment else None,
                              "start_time": row.start_time,
                              "end_time": row.end_time,
                          } for row in rows],
                          reason="该日期有多条可取消预约，请先展示全部候选")
            return result
    if draft is None:
        return result
    if selected and draft.status == "submitted":
        result.update(error="这组预约已经处理，请查询最新预约状态", authorized=False, allowed_write_tools=[])
    elif selected and draft.status == "ready":
        ctx.cancel_revision = draft.revision
        result.update(
            authorized=True,
            allowed_write_tools=["cancel_reservations"],
            cancel_target_ids=selected,
            cancel_revision=draft.revision,
            plan_needed=False,
            reason="用户已从会话内待取消候选中选择目标",
        )
    elif re.search(r"不要取消|别取消|算了|先不取消|改约|帮我预约", query):
        cancellations.discard(ctx.db, draft)
    return result


def _cancel_date(state: AgentState) -> str | None:
    query = state.get("user_query") or ""
    today = state.get("today") or datetime.now().strftime("%Y-%m-%d")
    spoken = planner._extract_date(query, today)
    month_day = re.search(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", query)
    if month_day and not spoken:
        try:
            year = int(today[:4])
            candidate = datetime(year, int(month_day.group(1)), int(month_day.group(2)))
            if candidate.date() < datetime.strptime(today, "%Y-%m-%d").date():
                candidate = candidate.replace(year=year + 1)
            spoken = candidate.strftime("%Y-%m-%d")
        except ValueError:
            return None
    return spoken or (state.get("slots") or {}).get("date")


def _save_cancel_candidates(ctx: AgentContext, state: AgentState, answer: str) -> None:
    if not isinstance(ctx.db, Session) or planner.explicit_write_request(state.get("user_query") or "") != "cancel_reservation":
        return
    if any(item.get("tool") == "cancel_reservation" and item.get("ok") for item in state.get("tool_results") or []):
        return
    date = _cancel_date(state)
    if not date:
        return
    rows = ctx.db.query(Reservation).filter_by(user_id=ctx.user.id, date=date, status=0).order_by(Reservation.id).all()
    ids = [row.id for row in rows]
    # 只有真正展示给用户的完整候选组才能成为下一轮的授权目标。
    if 1 < len(ids) <= 20 and all(re.search(rf"(?<!\d){item}(?!\d)", answer) for item in ids):
        cancellations.save(ctx.db, ctx.user.id, ctx.conversation_id, ids)


def _prepare_reservation_draft(
    ctx: AgentContext, state: AgentState, result: dict
) -> dict:
    """只让明确预约指令、对已有草稿的补充或确认继承授权。"""
    query = state.get("user_query") or ""
    wanted = planner.explicit_write_request(query)
    current = drafts.load(ctx.db, ctx.user.id, ctx.conversation_id)
    slots = result.get("slots") or {}
    if wanted == "cancel_reservation":
        if current and current.status == "ready":
            drafts.discard(ctx.db, current)
        return result
    if current and current.status == "submitting":
        result["error"] = "上一条预约正在提交中，请先查询预约结果，避免重复提交。"
        result["allowed_write_tools"] = []
        result["authorized"] = False
        return result
    if current and current.status == "submitted" and wanted == "create_reservation":
        if drafts.matches(current.target or {}, drafts.target_from(slots)):
            result["error"] = (
                f"这条预约已经提交（编号 {current.reservation_id}），无需重复提交。"
            )
            result["allowed_write_tools"] = []
            result["authorized"] = False
            return result
    if current and current.status == "submitted" and _CONFIRM_BOOKING.fullmatch(query):
        result["error"] = (
            f"这条预约已经提交（编号 {current.reservation_id}），无需重复提交。"
        )
        result["allowed_write_tools"] = []
        result["authorized"] = False
        return result
    confirmation = bool(_CONFIRM_BOOKING.fullmatch(query)) or (
        result.get("write_followup") == "booking_confirm"
        and bool(_BOOKING_FOLLOWUP.fullmatch(query))
    )
    if confirmation and (current is None or current.status != "ready"):
        result["error"] = "没有可确认的预约，或预约草稿已过期；请重新说明实验室和时段。"
        result["allowed_write_tools"] = []
        result["authorized"] = False
        return result
    missing_before = [
        field
        for field in ("date", "start_time", "end_time")
        if current and not (current.target or {}).get(field)
    ]
    followup = bool(
        current
        and current.status == "ready"
        and current.requested
        and missing_before
        and _SLOT_REPLY.fullmatch(query)
    )
    if not (wanted == "create_reservation" or confirmation or followup):
        if (
            current
            and current.status == "ready"
            and query.strip(" 。！!") not in {"确认", "好的", "可以", "嗯"}
        ):
            drafts.discard(ctx.db, current)
        return result
    if wanted != "create_reservation" and (
        current is None or current.status != "ready"
    ):
        return result

    target = dict(current.target or {}) if current and current.status == "ready" else {}
    if not confirmation:
        fresh = drafts.target_from(slots)
        if wanted == "create_reservation":
            if not _EXPLICIT_RANGE.search(query):
                # 模型可能从历史或“下午”默认值补出时段；写库前必须有明确时段。
                fresh.pop("start_time", None)
                fresh.pop("end_time", None)
            # 本轮明确说出的完整目标优先；不能让另一笔旧草稿污染新请求。
            changed_target = any(
                fresh.get(field) and target.get(field) and fresh[field] != target[field]
                for field in ("lab_name", "date")
            )
            if changed_target or (
                fresh.get("lab_name") and fresh.get("date") and fresh.get("start_time")
            ):
                target = fresh
            else:
                target.update(fresh)
        else:
            # 只填缺少的字段；覆盖已有日期/时段需用户明确重新下达预约指令。
            if not target.get("date") and planner._extract_date(
                query, state.get("today") or datetime.now().strftime("%Y-%m-%d")
            ):
                target["date"] = fresh.get("date", "")
            if re.search(r"[一二三四五六七八九十两\d]{1,3}\s*(?:点|时|:|：)", query):
                for field in ("start_time", "end_time"):
                    if not target.get(field) and fresh.get(field):
                        target[field] = fresh[field]
    if not target or (confirmation and not drafts.complete(target)):
        result["allowed_write_tools"] = []
        result["authorized"] = False
        return result
    if not confirmation:
        current = drafts.save(
            ctx.db, ctx.user.id, ctx.conversation_id, target, requested=True
        )
    if current is None or current.status != "ready":
        result["allowed_write_tools"] = []
        result["authorized"] = False
        return result
    target = dict(current.target or {})
    result["slots"] = {**slots, **target}
    result["missing_slots"] = [
        field for field in result.get("missing_slots", []) if field not in target
    ]
    # 部分指令只开放一个未完成的草稿；完整确认/补充时间才开放真正写库工具。
    if drafts.complete(target) and (
        wanted == "create_reservation" or confirmation or followup
    ):
        result["allowed_write_tools"] = ["create_reservation"]
        result["authorized"] = True
        result["draft_target"] = target
        result["draft_revision"] = current.revision
        result["plan_needed"] = False
        result["reason"] = "已确认会话内预约目标，重新校验后提交"
    else:
        result["allowed_write_tools"] = []
        result["authorized"] = False
        result["draft_target"] = target
        result["draft_pending"] = True
        result["missing_slots"] = [
            field for field in drafts.REQUIRED if not target.get(field)
        ]
    return result


def _save_available_draft(ctx: AgentContext, state: AgentState) -> None:
    """查询得到唯一明确的可用时段后，供下一轮简短确认。"""
    if not isinstance(ctx.db, Session) or state.get("error"):
        return
    candidates = []
    for item in state.get("tool_results") or []:
        if item.get("tool") != "query_lab_availability" or not item.get("ok"):
            continue
        checked = (item.get("result") or {}).get("requested") or {}
        if checked.get("available") is not True:
            continue
        args = item.get("args") or {}
        target = drafts.target_from({**(state.get("slots") or {}), **args})
        if drafts.complete(target):
            candidates.append(target)
    if len(candidates) != 1 or state.get("allowed_write_tools"):
        return
    current = drafts.load(ctx.db, ctx.user.id, ctx.conversation_id)
    if current and current.status in ("submitting", "submitted"):
        return
    drafts.save(
        ctx.db,
        ctx.user.id,
        ctx.conversation_id,
        candidates[0],
        requested=bool(current and current.requested),
    )


# ---------------------------------------------------------------------------
# 节点实现
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _async_trace(ctx: AgentContext, name: str, input_text: str = ""):
    # trace_node enters with only a timer/event; its exit writes to the DB.
    manager = trace_node(ctx, name, input_text=input_text)
    box = manager.__enter__()
    try:
        yield box
    except BaseException as exc:
        if not await run_sync(manager.__exit__, type(exc), exc, exc.__traceback__):
            raise
    else:
        await run_sync(manager.__exit__, None, None, None)


def _prepare_drafts(ctx: AgentContext, state: AgentState, result: dict) -> dict:
    result = _prepare_reservation_draft(ctx, state, result)
    return _prepare_cancel_draft(ctx, state, result)


def node_analyze(ctx: AgentContext):
    async def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        async with _async_trace(
            ctx, "analyze", input_text=state.get("user_query") or ""
        ) as box:
            result = await planner.analyze(ctx, state)
            if not result.get("error") and isinstance(ctx.db, Session):
                result = await run_sync(_prepare_drafts, ctx, state, result)
            # 轨迹是给人看的，同样不能出现裸字段名
            box["output"] = (
                f"槽位={result['slots']}；"
                f"缺失={planner.missing_labels(result['missing_slots']) or '无'}；"
                f"授权={result['authorized']}"
            )
            if result.get("error"):
                # 模型不可用。这一版没有规则兜底路径，所以把它当成一个结论往下传，
                # 由 _after_analyze 直接转去回复节点。
                box["output"] = result["error"]
            ctx.emit(
                {
                    "type": "analysis",
                    "slots": result["slots"],
                    "missing_slots": result["missing_slots"],
                    "authorized": result["authorized"],
                    "plan_needed": bool(result.get("plan_needed")),
                    "detail": result.get("reason") or box["output"],
                }
            )
            return result

    return run


def node_plan(ctx: AgentContext):
    """拆解任务计划。只有 analyze 判定 plan_needed=True 时才会到达这里。

    这是 v3 新增的节点，也是「条件触发」的落点：单查询 / 闲聊根本不会走到它，
    所以对那类请求来说，v3 的拓扑与 v2 完全一致。
    """

    async def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        async with _async_trace(ctx, "plan", input_text=state.get("user_query") or "") as box:
            result = await planner.make_plan(ctx, state)
            plan = result.get("plan") or []
            box["output"] = (
                planner.plan_steps_text(plan) or "未能生成计划，退回单步执行"
            )
            ctx.emit(
                {
                    "type": "plan",
                    "status": "created",
                    "revision": 0,
                    "cursor": 0,
                    "steps": plan,
                    "reason": result.get("plan_reason") or "",
                }
            )
            # 注意这里**不置 error**：规划失败只是「这次没有计划」，
            # 不代表「答不出来」。_after_plan 会把空计划送回 agent ⇄ execute
            # 快路径 —— 退化成 v2 的行为，而不是向用户报错。
            return result

    return run


def node_replan(ctx: AgentContext):
    """按已拿到的观察结果重排剩余步骤。

    调用前提是 _replan_needed() 已经确认「有一步没做成、且有可行的替代工具」。
    """

    async def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        async with _async_trace(ctx, "replan", input_text=state.get("user_query") or "") as box:
            result = await planner.revise_plan(ctx, state)
            revision = result.get("plan_revision") or 0
            plan = result.get("plan") or []
            if plan:
                box["output"] = (
                    f"第 {revision} 次调整：\n{planner.plan_steps_text(plan)}"
                )
            else:
                box["output"] = "重规划未产出可用步骤，保持原计划继续"
            ctx.emit(
                {
                    "type": "plan",
                    "status": "revised",
                    "revision": revision,
                    # 重规划后游标**停在原处**：被替换掉的那一步在新计划里的位置
                    # 正好就是第一份「剩余步骤」，所以 plan_cursor 依然是
                    # 「下一个要跑的步骤下标」。前端靠它把已经完成的步骤保留下来。
                    "cursor": state.get("plan_cursor") or 0,
                    "steps": plan,
                    "reason": result.get("plan_reason") or "",
                }
            )
            return result

    return run


def node_advance(ctx: AgentContext):
    """一步收尾：把本步快照记账、游标推到下一步。

    刻意**不**包 trace_node：它是纯管道（不调模型、不碰数据库），在轨迹面板上
    多一格「推进到下一步」只会稀释真正有信息量的那几格。但它仍会 emit 一条
    plan 事件，让前端能把这一步标记成完成。
    """

    def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        cursor = state.get("plan_cursor") or 0
        plan = list(state.get("plan") or [])
        step = (plan[cursor] if cursor < len(plan) else {}) or {}
        results = _step_results(state, cursor)
        ok = _step_ok(results)
        snapshot = {
            "index": cursor + 1,
            "goal": step.get("goal") or "",
            "ok": ok,
            "calls": [item.get("tool") for item in results],
            "note": "" if ok else "这一步没有取得有效结果",
        }
        recorded = list(state.get("step_results") or [])
        recorded.append(snapshot)

        ctx.emit(
            {
                "type": "plan",
                "status": "step_done",
                "cursor": cursor + 1,
                "ok": ok,
                "goal": snapshot["goal"],
            }
        )

        return {
            "step_results": recorded,
            "plan_cursor": cursor + 1,
            "step_round": 0,
        }

    return run


def node_agent(ctx: AgentContext):
    """自主决策：问模型一次，把它要调的工具登记下来。

    这里是整个改造的核心 —— 调哪个工具、带什么参数，完全由模型根据用户原话
    和工具描述自己决定。代码只负责把它的 tool_calls 拿去执行。
    """

    async def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        round_no = state.get("tool_round") or 0
        step_round = state.get("step_round") or 0
        plan = list(state.get("plan") or [])
        cursor = state.get("plan_cursor") or 0

        # 当前步骤的建议工具。计划里 hint_tools 为空（或没启用计划）时为 None，
        # 表示「本步不限制工具」—— bindable_tools 只在 only 非空时才收窄。
        hint_tools: set[str] | None = None
        if plan and cursor < len(plan):
            hinted = (plan[cursor] or {}).get("hint_tools") or []
            if hinted:
                hint_tools = set(hinted)

        # 两道刹车：
        #   - 全局 tool_round 用尽 → 整轮不再调工具；
        #   - 本步 step_round 用尽 → 只收本步的工具，防止多步计划里
        #     某一步反复重试把全局预算吃光，让后面的步骤一步都拿不到工具。
        exhausted = round_no >= MAX_TOOL_ROUNDS or step_round >= STEP_TOOL_ROUNDS

        # 本轮实际交给模型的工具名。和 agent_step 内部各算一次是有意的：
        # 这个函数要能算出「会不会退回全集」，改动绑定逻辑时两处必须一起改，
        # 但比把 agent_step 的返回值改成元组（会牵动所有调用方和测试）划算。
        bound_tools = tools.bindable_tool_names(
            set(state.get("allowed_write_tools") or []), only=hint_tools
        )

        async with _async_trace(ctx, "agent", input_text=state.get("user_query") or "") as box:
            try:
                message = await planner.agent_step(
                    ctx,
                    state,
                    tool_choice="none" if exhausted else None,
                    hint_tools=hint_tools,
                )
            except Exception:
                # 模型在这一轮挂了。不再有确定性兜底（那是上一版的设计），
                # 但不兜底不等于不解释 —— 交给回复节点如实说明。
                logger.warning("自主决策调用失败，改为如实告知用户", exc_info=True)
                return {"error": planner.MODEL_UNAVAILABLE, "pending_calls": []}

            calls = [
                {
                    "id": call.get("id") or f"call_{round_no}_{index}",
                    "step": (round_no * 10) + index,
                    "name": call.get("name") or "",
                    "args": call.get("args") or {},
                }
                for index, call in enumerate(message.tool_calls, start=1)
            ]

            text = planner.message_text(message).strip()
            if calls and text:
                # 模型在调工具前先说了一句话。提示词里明令禁止，但真发生时不能把它
                # 当最终回答 —— 这时候它还没看到工具结果，说出来的多半是
                # 「我这就去查」这类铺垫。前端可能已经把这段显示出来了，
                # 所以送一个 reset 让它清掉。
                ctx.emit({"type": "reset"})

            # 轨迹文案带上「第几步」——多步计划里光看「第 3 轮」看不出在做什么。
            prefix = (
                f"第 {cursor + 1} 步 · 第 {round_no + 1} 轮："
                if plan and cursor < len(plan)
                else f"第 {round_no + 1} 轮："
            )
            box["output"] = prefix + (
                "调用 " + "、".join(call["name"] for call in calls)
                if calls
                else "给出结论"
            )

        # 每个待调用的工具先落一条 running 事件：前端据此把「正在调用…」
        # 那行先摆出来，等 execute 回填结果。
        for call in calls:
            ctx.emit(
                {
                    "type": "step",
                    "id": call["step"],
                    "tool": call["name"],
                    "label": tools.tool_label(call["name"]),
                    "status": "running",
                    "args": call["args"],
                }
            )

        return {
            "messages": [message],
            "pending_calls": calls,
            "bound_tools": bound_tools,
            "tool_round": round_no + 1,
            "step_round": step_round + 1,
        }

    return run


def node_execute(ctx: AgentContext):
    """执行模型点名的工具，把结果作为 ToolMessage 交回去。"""

    def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        calls = list(state.get("pending_calls") or [])
        if not calls:
            return {}

        allowed_writes = set(state.get("allowed_write_tools") or [])
        results = list(state.get("tool_results") or [])
        messages: list = []
        step_index = len(results)
        # 每条工具结果都带上「属于计划里的哪一步」。
        # 为什么要在结果上记这个：重规划靠它区分「本步的失败」与「上一步的历史」，
        # 幂等闸门也只认「本步之前是否已经成功做过同样的写操作」。
        # 快路径下 cursor 恒为 0，这个字段没有任何副作用。
        cursor = state.get("plan_cursor") or 0
        # 本轮真正交到模型手里的工具名。即使状态缺失也按空清单处理，
        # 执行边界必须在失败时收紧，不能默认放行。
        bound = set(state.get("bound_tools") or [])

        with trace_node(
            ctx,
            "execute",
            input_text="；".join(f"{call['name']}({call['args']})" for call in calls),
        ) as box:
            for call in calls:
                tool_name = call["name"]
                args = call["args"]
                step_index += 1
                status = "success"

                if tool_name not in bound:
                    # 越界调用：模型点了一个这一轮根本没提供给它的工具。
                    # 部分 OpenAI 兼容网关对 tools 参数并不严格，实测模型确实会
                    # 吐出清单外的名字，所以 bind_tools 那层约束是不完备的。
                    # 不执行、如实拒绝，并把「本步只能用哪些」回给模型 ——
                    # 它下一轮就能自己纠正，而不是让我们默默替它兜底。
                    logger.warning(
                        "拦截越出本轮工具清单的调用：%s（本步可用：%s）",
                        tool_name,
                        "、".join(sorted(bound)),
                    )
                    result = {
                        "ok": False,
                        "error": (
                            f"本轮没有向模型提供工具 {tool_name}，"
                            f"当前可用工具：{'、'.join(sorted(bound))}"
                        ),
                    }
                    elapsed = 0.0
                elif tool_name in WRITE_TOOLS and tool_name not in allowed_writes:
                    logger.warning("拦截未授权的写库调用：%s", tool_name)
                    result = {"ok": False, "error": UNAUTHORIZED_WRITE}
                    elapsed = 0.0
                elif (
                    tool_name == "cancel_reservations"
                    and (
                        not state.get("cancel_target_ids")
                        or sorted(args.get("reservation_ids") or []) != sorted(state["cancel_target_ids"])
                        or not (draft := cancellations.load(ctx.db, ctx.user.id, ctx.conversation_id))
                        or draft.revision != state.get("cancel_revision")
                    )
                ):
                    result = {"ok": False, "error": "待取消目标与本轮确认内容不一致"}
                    elapsed = 0.0
                elif (
                    tool_name == "create_reservation"
                    and state.get("draft_target")
                    and (
                        not isinstance(ctx.db, Session)
                        or not (
                            draft := drafts.load(
                                ctx.db, ctx.user.id, ctx.conversation_id
                            )
                        )
                        or draft.revision != state.get("draft_revision")
                        or not drafts.matches(state["draft_target"], args)
                    )
                ):
                    result = {
                        "ok": False,
                        "error": "待确认预约已变更或参数不一致，请重新核对预约目标",
                    }
                    elapsed = 0.0
                elif tools.is_duplicate_write(tool_name, args, results):
                    # 幂等闸门：重规划之后，模型很可能把「已经成功的那次写库」
                    # 又安排一遍。写库不可撤回，重复创建就是两条真实数据。
                    # 这里直接跳过，并把「已经做过了」作为观察结果交回去 ——
                    # 模型看到的是事实，而不是一个语焉不详的失败。
                    logger.warning(
                        "跳过重复的写库调用：%s(%s)（本轮已成功执行过）",
                        tool_name,
                        args,
                    )
                    result = {
                        "ok": True,
                        "duplicate": True,
                        "message": "该操作本轮已成功执行过，未重复提交",
                    }
                    elapsed = 0.0
                    status = "skipped"
                else:
                    # 工具级计时与节点级计时是两回事：一个 execute 节点的耗时里
                    # 还包含 emit、写审计这些开销，而审计关心的是
                    # 「这个工具本身多慢」。
                    started = time.perf_counter()
                    draft = None
                    if tool_name == "create_reservation" and state.get("draft_target"):
                        draft = drafts.load(ctx.db, ctx.user.id, ctx.conversation_id)
                        claimed, reason = drafts.claim(ctx.db, draft, args)
                        result = (
                            tools.run_tool(ctx, tool_name, args)
                            if claimed
                            else {"ok": False, "error": reason}
                        )
                        if (
                            claimed
                            and result.get("ok")
                            and result.get("reservation_id")
                        ):
                            drafts.finish(ctx.db, draft, result["reservation_id"])
                        elif claimed and result.get("ok") is False:
                            # 工具可能在服务层 commit 后报错；先查记录再决定能否重试。
                            existing = drafts.find_existing(ctx.db, draft)
                            if existing and existing.id > (
                                draft.prior_reservation_id or 0
                            ):
                                drafts.finish(ctx.db, draft, existing.id)
                                result = {
                                    "ok": True,
                                    "reservation_id": existing.id,
                                    **draft.target,
                                    "status": "待审核",
                                    "message": "预约已提交，数据库核验成功",
                                }
                            else:
                                drafts.release(ctx.db, draft)
                    else:
                        result = tools.run_tool(ctx, tool_name, args)
                    elapsed = (time.perf_counter() - started) * 1000

                failed = result.get("ok") is False
                if failed:
                    status = "failed"
                summary = tools.summarize(tool_name, result)
                results.append(
                    {
                        "tool": tool_name,
                        "args": args,
                        "ok": not failed,
                        "result": result,
                        "step": cursor,
                    }
                )

                # 工具级审计记录。与节点轨迹分开写，因为两者回答的是不同问题：
                # agent_trace 回答「Agent 想了什么」，这张表回答「Agent 做了什么、
                # 带了什么参数、结果是什么、多慢」—— 后者才是「Tool Calling 不是
                # 黑盒」的直接证据，也是排查「模型总把参数填错」时唯一能算的数据源。
                write_tool_execution(
                    ctx,
                    tool_name=tool_name,
                    tool_label=tools.tool_label(tool_name),
                    tool_input=args,
                    tool_output=result,
                    observation=summary,
                    step_index=step_index,
                    status=status,
                    execution_time=elapsed,
                    error=None if not failed else str(result.get("error") or ""),
                )

                ctx.emit(
                    {
                        "type": "step",
                        "id": call["step"],
                        "tool": tool_name,
                        "label": tools.tool_label(tool_name),
                        "status": "failed" if failed else "done",
                        "detail": summary,
                        "result": result,
                        "duration_ms": round(elapsed, 1),
                    }
                )

                # 交回给模型的观察结果。注意这里给的是 summarize 而不是原始 JSON：
                # 原始 JSON 动辄几千字符，塞进上下文只会把真正有用的字段挤出去。
                messages.append(ToolMessage(content=summary, tool_call_id=call["id"]))

                # 预约成功后把偏好沉淀进长期记忆。
                # 只对「创建」做，取消不该反过来污染偏好 ——
                # 用户取消一次不等于他不再喜欢那个时段。
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
                    except Exception:
                        # 学偏好失败不能影响主流程
                        logger.exception("写入长期记忆失败")

            box["output"] = "；".join(
                f"{item['tool']}：{'成功' if item['ok'] else '失败'}"
                for item in results[-len(calls) :]
            )

        return {
            "messages": messages,
            "pending_calls": [],
            "tool_results": results,
        }

    return run


def node_respond(ctx: AgentContext):
    """把结论吐给用户。

    正常情况下，就是模型最后那条带文字的回复 —— 这一版它是**看过工具结果之后**
    自己写的，所以不需要再拿渲染好的执行日志去合成一次。
    只有模型在半路挂掉时（state["error"]）才换成一句交代，
    但已经真落库的写操作必须一并讲清楚（见 planner.write_outcomes）。
    """

    def run(state: AgentState) -> dict:
        _bind_writer(ctx)
        with trace_node(
            ctx, "respond", input_text=state.get("user_query") or ""
        ) as box:
            text = (state.get("error") or "").strip()
            if text:
                facts = planner.write_outcomes(state.get("tool_results"))
                if facts:
                    text = (
                        f"{text}\n\n⚠️ 但请注意，本轮已经有操作真实落库了：\n"
                        + "\n".join(f"· {line}" for line in facts)
                    )
            elif state.get("cancel_pending") and state.get("cancel_candidate_details"):
                rows = state["cancel_candidate_details"]
                lines = [
                    f"- 预约号 {row['reservation_id']}：{row['lab_name']}"
                    + (f" · {row['equipment_name']}" if row.get("equipment_name") else "")
                    + f" · {row['start_time']}–{row['end_time']}（待审核）"
                    for row in rows
                ]
                text = "这一天有多条可取消的预约：\n\n" + "\n".join(lines) + "\n\n请告诉我预约号或时段；也可以说「都取消」。"
            else:
                text = _last_ai_text(state)
            if not text:
                text = planner.EMPTY_REPLY

            _save_available_draft(ctx, state)
            _save_cancel_candidates(ctx, state, text)
            ctx.emit({"type": "token", "content": text})
            box["output"] = text
            return {"final_response": text}

    return run


def _last_ai_text(state: AgentState) -> str:
    """取模型最后一条「真的在说话」的回复。

    只能认 AIMessage：state["messages"] 里还夹着 ToolMessage（工具观察结果），
    以及模型那几条只带 tool_calls、没带文字的空消息。
    """
    for message in reversed(state.get("messages") or []):
        if isinstance(message, AIMessage):
            text = planner.message_text(message).strip()
            if text:
                return text
    return ""


# ---------------------------------------------------------------------------
# 条件边
# ---------------------------------------------------------------------------


def _after_analyze(state: AgentState) -> str:
    """模型不可用就不去碰自主决策 —— 那里只会再多花一次超时。

    否则看 analyze 的判定：需要规划就先去 plan，不需要就直接进 agent。
    ⚠️ 这个分支只看 plan_needed 这个**模型给出的结论**，代码里没有任何
    「用户说了『然后』就规划」这类关键词判断 —— 本项目明令禁止用关键词
    驱动流程控制。
    """
    if state.get("error"):
        return "respond"
    if state.get("plan_needed"):
        return "plan"
    return "agent"


def _after_plan(state: AgentState) -> str:
    """规划完就一定进 agent。

    计划为空（模型没给出可用步骤）时也进 agent —— 那一轮就退化成
    v2 的单步 ReAct 路径，而不是停下来报错：规划失败只影响「按不按计划走」，
    不影响「能不能答」。
    """
    return "respond" if state.get("error") else "agent"


def _step_results(state: AgentState, cursor: int) -> list[dict]:
    """取出「属于第 cursor 步」的工具结果。"""
    return [
        item for item in (state.get("tool_results") or []) if item.get("step") == cursor
    ]


def _step_ok(results: list[dict]) -> bool:
    """这一步算不算成功。

    两条判据：
      - 一个工具都没调 → 算成功（模型直接给了结论，比如最后那步汇总）；
      - 调了工具 → 只要有**一个**成功就算成功。
    第二条很关键：一步里「查 A 失败 → 改查 B 成功」是正常的探索，
    不能因为中间失败过一次就判整步失败。
    """
    if not results:
        return True
    return any(item.get("ok") for item in results)


def _replan_needed(state: AgentState) -> bool:
    """重规划的**确定性闸门** —— 这是 v1 §7.6 教训的落点。

    只有同时满足以下全部条件才允许让模型重排计划：
      1. 有计划、且没有全局错误；
      2. 还没用满 MAX_REPLAN_ROUNDS 次；
      3. 当前步真的没做成（调过工具，但一个成功的都没有）；
      4. 存在「本步没用过、且本轮可用」的工具，即确实有补救路径。
    其余一律 False。

    ⚠️ 第 3 条是整个函数的重点：**做成了就绝不重规划**。
    v1 的反射节点会在步骤全部成功之后还去问模型「要不要调整」，
    结果是把已经答对的题改错了。这里把「成功」硬编码成 False，
    让重规划在结构上不可能发生在那条路径上。
    """
    if state.get("error"):
        return False
    plan = state.get("plan") or []
    if not plan:
        return False
    if (state.get("plan_revision") or 0) >= MAX_REPLAN_ROUNDS:
        return False

    cursor = state.get("plan_cursor") or 0
    current = _step_results(state, cursor)
    if _step_ok(current):
        return False

    # 有失败、也有备用工具，才值得让模型重排一次；
    # 否则重规划只会得到一份一模一样的计划。
    allowed_writes = set(state.get("allowed_write_tools") or [])
    available = {item.name for item in tools.bindable_tools(allowed_writes)}
    used = {item.get("tool") for item in current}
    return bool(available - used)


def _after_agent(state: AgentState) -> str:
    """模型要了工具就去执行；没要就「本步收尾」——重规划、推进、或回复。"""
    if state.get("error"):
        return "respond"
    if state.get("pending_calls") or []:
        if (state.get("tool_round") or 0) > MAX_TOOL_ROUNDS:
            # 轮次已经用尽，模型还在要工具（说明 tool_choice="none" 被忽略了）。
            # 不再执行、也不再问 —— 直接收尾，循环必须有硬上限。
            logger.warning("工具轮次已用尽，模型仍在请求调用，强制收尾")
            return "respond"
        return "execute"

    # ---- 模型没要工具：这一步（或这一轮）结束了 ----
    plan = state.get("plan") or []
    if not plan:
        # 快路径：与 v2 完全一致，直接去回复。
        return "respond"
    if _replan_needed(state):
        return "replan"
    cursor = state.get("plan_cursor") or 0
    if cursor + 1 < len(plan):
        return "advance"
    return "respond"


# ---------------------------------------------------------------------------
# 图构建与执行
# ---------------------------------------------------------------------------


def _worker_node(handler):
    async def run(state: AgentState) -> dict:
        return await run_sync(handler, state)

    return run


def build_graph(ctx: AgentContext):
    graph = StateGraph(AgentState)

    graph.add_node("analyze", node_analyze(ctx))
    graph.add_node("plan", node_plan(ctx))
    graph.add_node("agent", node_agent(ctx))
    graph.add_node("execute", _worker_node(node_execute(ctx)))
    graph.add_node("advance", node_advance(ctx))
    graph.add_node("replan", node_replan(ctx))
    graph.add_node("respond", _worker_node(node_respond(ctx)))

    graph.add_edge(START, "analyze")
    # analyze 之后三选一：模型不可用 → respond；需要规划 → plan；否则快路径 → agent。
    graph.add_conditional_edges(
        "analyze",
        _after_analyze,
        {"plan": "plan", "agent": "agent", "respond": "respond"},
    )
    graph.add_conditional_edges(
        "plan", _after_plan, {"agent": "agent", "respond": "respond"}
    )
    # agent 之后四选一：要工具 → execute；本步结束且需调整 → replan；
    # 本步结束且还有下一步 → advance；否则 → respond。
    graph.add_conditional_edges(
        "agent",
        _after_agent,
        {
            "execute": "execute",
            "advance": "advance",
            "replan": "replan",
            "respond": "respond",
        },
    )
    graph.add_edge("execute", "agent")
    graph.add_edge("advance", "agent")
    graph.add_edge("replan", "agent")
    graph.add_edge("respond", END)

    return graph.compile()


def _initial_state(ctx: AgentContext, query: str, history: list[dict]) -> AgentState:
    """构造初始状态。

    memory_context 在这里就读出来，而不是等回复节点再读：
    1. 只读一次库，避免每个节点都查一遍；
    2. 自主决策也要看得到偏好 ——「这位用户习惯约下午」会影响它挑哪个时段、
       也会影响它在缺信息时先查什么。
    """
    try:
        memory_context = memory.load_memory_context(ctx.db, ctx.user.id)
    except Exception:
        logger.exception("读取长期记忆失败")
        memory_context = "（记忆读取失败）"

    return AgentState(
        user_id=ctx.user.id,
        conversation_id=ctx.conversation_id,
        today=datetime.now().strftime("%Y-%m-%d"),
        user_query=query,
        history=history,
        memory_context=memory_context,
        messages=[],
        tool_round=0,
        pending_calls=[],
        bound_tools=[],
        tool_results=[],
        # 规划层的初始值。快路径下这几个字段全程不变 ——
        # plan 为空是 _after_agent 判定「走快路径」的依据。
        plan=[],
        plan_needed=False,
        plan_cursor=0,
        plan_revision=0,
        plan_reason="",
        step_round=0,
        step_results=[],
        allowed_write_tools=[],
        final_response="",
    )


def resolve_history(conversation_id: str, history: list[dict] | None) -> list[dict]:
    """决定这一轮用哪份对话历史。

    调用方（前端）带上来的历史优先：它是抗刷新、抗重启的那一份记录。
    进程内记忆只在请求没带历史时兜底 —— 它一重启就清空，而开发环境跑的是
    `--reload`，改任何一个 .py 都会重启，用户看到的现象就是「上一轮刚说过的
    实验室，这一轮又问我一遍」。
    """
    if history:
        return history
    return memory.get_history(conversation_id)


class WorkflowRunner:
    """一次问答的执行器。流式和非流式共用同一张图。"""

    def __init__(
        self,
        db: Session,
        user: User,
        conversation_id: str,
        query: str,
        history: list[dict] | None = None,
    ):
        self.ctx = AgentContext(db=db, user=user, conversation_id=conversation_id)
        self.query = query
        self.history = resolve_history(conversation_id, history)
        self.state = _initial_state(self.ctx, query, self.history)
        self.graph = build_graph(self.ctx)

    # -- 流式 ------------------------------------------------------------
    async def astream(self) -> AsyncIterator[dict]:
        with request_tracing(self.ctx.conversation_id, "stream", RECURSION_LIMIT) as config:
            stream = self._astream(config)
            try:
                async for event in stream:
                    yield event
            finally:
                await stream.aclose()

    async def _astream(self, config: RunnableConfig) -> AsyncGenerator[dict, None]:
        """跑工作流并把每个节点产生的事件实时吐出去。

        用 stream_mode="custom"：节点内部通过 get_stream_writer() 主动推送事件，
        比解析 updates 更灵活（可以把 token 粒度的事件也带出来）。
        """
        final_state: dict = dict(self.state)
        # langgraph 的 astream 存根有多组 overload，Pyright 选中的那组把
        # stream_mode=["custom", "values"] 的返回标成了 AsyncIterator[dict]，
        # 解包出来的 payload 因此不再是 dict。按真实形态断言回来。
        stream = cast(
            "AsyncGenerator[tuple[str, Any], None]",
            self.graph.astream(
                self.state,
                stream_mode=["custom", "values"],
                config=config,
            ),
        )
        try:
            async for mode, payload in stream:
                if mode == "custom":
                    yield payload
                else:
                    # values 模式每次给出完整状态，留最后一份即可拿到 final_response
                    final_state = payload
        except Exception:
            logger.exception("Agent 工作流执行失败")
            yield {"type": "error", "message": "Agent 执行失败，请稍后重试"}
            return
        finally:
            await stream.aclose()

        answer = (final_state or {}).get("final_response") or ""
        memory.append_turn(self.ctx.conversation_id, self.query, answer)
        yield {"type": "done", "answer": answer}

    # -- 非流式 ----------------------------------------------------------
    async def ainvoke(self) -> str:
        try:
            with request_tracing(self.ctx.conversation_id, "invoke", RECURSION_LIMIT) as config:
                result = await self.graph.ainvoke(self.state, config=config)
        except Exception:
            logger.exception("Agent 工作流执行失败")
            raise

        answer = (result or {}).get("final_response") or ""
        memory.append_turn(self.ctx.conversation_id, self.query, answer)
        return answer
