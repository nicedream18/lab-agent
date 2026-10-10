"""Agent 执行轨迹的记录与查询。

单独成一个模块（而不是塞进 graph.py 或 memory.py）的理由：
- 它不是业务流程的一部分，而是横切关注点（cross-cutting concern）；
- 落库失败绝不能影响 Agent 主流程，这个容错逻辑只应该写一次；
- 前端 Trace Panel 和「历史轨迹查询」接口都依赖它，需要一个稳定边界。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from app.agent.state import NODE_LABELS, WRITE_TOOLS, AgentContext
from app.models.agent_tool_execution import AgentToolExecution
from app.models.agent_trace import AgentTrace

logger = logging.getLogger(__name__)

# 单条轨迹里输入输出的最大长度。轨迹是给人看的，
# 存一整份工具返回的 JSON 只会让 Trace Panel 没法读。
MAX_FIELD_CHARS = 2000


def _clip(text: str | None) -> str:
    value = text or ""
    if len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + "…"
    return value


def write_trace(
    ctx: AgentContext,
    node_name: str,
    input_text: str = "",
    output_text: str = "",
    status: str = "success",
    execution_time: float = 0.0,
) -> None:
    """写一条轨迹。任何异常都只记日志，不向上抛。"""
    try:
        item = AgentTrace(
            conversation_id=ctx.conversation_id,
            user_id=ctx.user.id,
            node_name=node_name,
            step_index=ctx.next_index(),
            status=status,
            input=_clip(input_text),
            output=_clip(output_text),
            execution_time=round(execution_time, 2),
        )
        ctx.db.add(item)
        ctx.db.commit()
    except Exception:
        # 轨迹写失败不能让整轮问答失败，回滚并继续
        ctx.db.rollback()
        logger.exception("写入 Agent 轨迹失败：node=%s", node_name)


@contextmanager
def trace_node(
    ctx: AgentContext, node_name: str, input_text: str = ""
) -> Generator[dict, None, None]:
    """包住一个节点，自动计时、落库、并向前端推 node 事件。

    用法：
        with trace_node(ctx, "plan", input_text=query) as box:
            ...
            box["output"] = "生成了 4 步计划"

    节点内部抛异常时，会把异常信息记进 output 并标记 failed，然后继续往外抛
    （失败要能被上层感知），但轨迹本身已经落库了，不会丢。
    """
    label = NODE_LABELS.get(node_name, node_name)
    ctx.emit({"type": "node", "node": node_name, "label": label, "status": "start"})
    started = time.perf_counter()
    box: dict = {"output": "", "status": "success"}

    try:
        yield box
    except Exception as exc:
        box["status"] = "failed"
        box["output"] = box["output"] or f"{type(exc).__name__}: {exc}"
        raise
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        write_trace(
            ctx,
            node_name,
            input_text=input_text,
            output_text=box["output"],
            status=box["status"],
            execution_time=elapsed,
        )
        ctx.emit(
            {
                "type": "node",
                "node": node_name,
                "label": label,
                "status": "end",
                "detail": box["output"],
                "state": box["status"],
                "duration_ms": round(elapsed, 1),
            }
        )


def load_trace(db: Session, conversation_id: str) -> list[AgentTrace]:
    """按执行顺序取回一次会话的完整轨迹。"""
    return (
        db.query(AgentTrace)
        .filter(AgentTrace.conversation_id == conversation_id)
        .order_by(AgentTrace.id.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# 工具级记录（agent_tool_execution）
# ---------------------------------------------------------------------------

# 单条工具记录的字段上限。工具返回值最长也就是 tools.MAX_RESULT_CHARS（1500），
# 这里给到 4000 是留给参数的余量 —— 参数通常很短，但 find_available_slots
# 这类工具的返回结构比较深，压太狠会让审计失去意义。
MAX_TOOL_FIELD_CHARS = 4000


def _clip_json(value: Any, limit: int = MAX_TOOL_FIELD_CHARS) -> str:
    """把参数/结果序列化成 JSON 文本并截断。

    刻意用 json.dumps 而不是 str()：这张表的定位是**可解析的审计记录**，
    下游可能真的会用 json.loads 去读它（比如统计「哪些参数最常被模型填错」）。
    序列化失败时退回 str()，绝不让记录本身成为故障点。
    """
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    if len(text) > limit:
        return text[:limit] + "…（已截断）"
    return text


def write_tool_execution(
    ctx: AgentContext,
    tool_name: str,
    tool_label: str | None,
    tool_input: Any,
    tool_output: Any,
    observation: str = "",
    step_index: int = 0,
    status: str = "success",
    execution_time: float = 0.0,
    error: str | None = None,
) -> None:
    """写一条工具调用记录。与轨迹一样，任何异常都只记日志、不向上抛。

    为什么要把落库和「工具是否成功」解耦：**工具失败本身正是最需要被记录的**
    ——「模型反复给 create_reservation 填错参数」这类问题，只有靠这张表
    按 tool_name + status 聚合才能看出来。所以 ok=False 也照写不误。

    status 取值：success / failed / skipped。
    skipped 专指「重规划后模型又安排了一次已经成功过的写库调用，被幂等闸门拦下」
    ——它也是审计要看的信号：出现 skipped 说明模型没记住自己已经做完了。
    """
    try:
        item = AgentToolExecution(
            conversation_id=ctx.conversation_id,
            user_id=ctx.user.id,
            step_index=step_index,
            tool_name=tool_name,
            tool_label=tool_label,
            tool_input=_clip_json(tool_input or {}),
            tool_output=_clip_json(tool_output or {}),
            observation=observation[:MAX_TOOL_FIELD_CHARS] if observation else None,
            is_write=1 if tool_name in WRITE_TOOLS else 0,
            status=status,
            execution_time=round(execution_time, 2),
            error=error,
        )
        ctx.db.add(item)
        ctx.db.commit()
    except Exception:
        # 审计记录写失败绝不能影响业务 —— 这是「观测不能改变被观测系统」
        ctx.db.rollback()
        logger.exception("写入工具执行记录失败：tool=%s", tool_name)


def load_tool_executions(db: Session, conversation_id: str) -> list[AgentToolExecution]:
    """按执行顺序取回一次会话的所有工具调用。"""
    return (
        db.query(AgentToolExecution)
        .filter(AgentToolExecution.conversation_id == conversation_id)
        .order_by(AgentToolExecution.id.asc())
        .all()
    )
