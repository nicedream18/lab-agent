"""Agent 执行轨迹的记录与查询。

单独成一个模块（而不是塞进 graph.py 或 memory.py）的理由：
- 它不是业务流程的一部分，而是横切关注点（cross-cutting concern）；
- 落库失败绝不能影响 Agent 主流程，这个容错逻辑只应该写一次；
- 前端 Trace Panel 和「历史轨迹查询」接口都依赖它，需要一个稳定边界。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.orm import Session

from app.agent.state import NODE_LABELS, AgentContext
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
    except Exception:  # noqa: BLE001
        # 轨迹写失败不能让整轮问答失败，回滚并继续
        ctx.db.rollback()
        logger.exception("写入 Agent 轨迹失败：node=%s", node_name)


@contextmanager
def trace_node(
    ctx: AgentContext, node_name: str, input_text: str = ""
) -> Iterator[dict]:
    """包住一个节点，自动计时、落库、并向前端推 node 事件。

    用法：
        with trace_node(ctx, "plan", input_text=query) as box:
            ...
            box["output"] = "生成了 4 步计划"

    节点内部抛异常时，会把异常信息记进 output 并标记 failed，然后继续往外抛
    （失败要能被上层感知），但轨迹本身已经落库了，不会丢。
    """
    label = NODE_LABELS.get(node_name, node_name)
    ctx.emit(
        {"type": "node", "node": node_name, "label": label, "status": "start"}
    )
    started = time.perf_counter()
    box: dict = {"output": "", "status": "success"}

    try:
        yield box
    except Exception as exc:  # noqa: BLE001
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
