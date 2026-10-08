"""Agent 门面（Facade）。

为什么还需要这一层？
API 层不应该知道 LangGraph 的存在 —— 它只关心「给我一句话，我还你一串事件」。
把会话 ID 生成、请求校验、异常兜底这些 HTTP 周边的琐事收在这里，
`app/agent/graph.py` 就能保持纯粹的「工作流」语义。

改造说明：本文件原先实现的是 ReAct 循环（agent ⇄ tools），
现已整体替换为 LangGraph 显式工作流，实现位于 `app/agent/graph.py`。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from app.agent import graph as workflow
from app.agent.state import MAX_HISTORY_MESSAGES
from app.common.exceptions import BusinessException
from app.models.user import User
from app.schemas.ai import ChatRequest

logger = logging.getLogger(__name__)


def ensure_conversation_id(data: ChatRequest) -> str:
    """保证请求一定带着 conversation_id（幂等）。

    短期记忆和 agent_trace 都按 conversation_id 归档，
    没有它整条链路就没法回溯，所以这里由后端兜底生成。
    """
    conversation_id = (data.conversation_id or "").strip()
    if not conversation_id:
        conversation_id = uuid.uuid4().hex
        data.conversation_id = conversation_id
    return conversation_id


def _last_user_query(data: ChatRequest) -> str:
    """取最后一句用户发言作为本轮 query。

    只取最后一句而不是拼接全部历史：历史由 memory 层按会话提供，
    拼接会导致同一个问题在提示词里重复出现，反而干扰模型判断。
    """
    for message in reversed(data.messages or []):
        if message.role == "user" and (message.content or "").strip():
            return message.content.strip()
    raise BusinessException(message="请输入您要对话的内容")


def _history_from_request(data: ChatRequest, query: str) -> list[dict] | None:
    """把前端带上来的历史对话整理成短期记忆（role/content 列表）。

    为什么以请求为准、而不只靠 memory 层的进程内会话：
    1. 进程内会话一重启就没了。开发环境跑的是 `--reload`，改任何一个 .py 都会重启，
       用户看到的现象就是「上一轮刚说过的实验室，这一轮又问我一遍」；
    2. 前端每轮都会把最近 N 条消息完整发上来，它才是抗刷新、抗重启的那份记录。
    进程内记忆仍然保留，作为请求没带历史时的兜底（见 graph.resolve_history）。
    """
    items = list(data.messages or [])
    # 最后一条就是本轮问题本身，不能混进历史，否则同一个问题会在提示词里出现两次
    if (
        items
        and items[-1].role == "user"
        and (items[-1].content or "").strip() == query
    ):
        items = items[:-1]

    history = [
        {"role": item.role, "content": (item.content or "").strip()}
        for item in items
        if item.role in ("user", "assistant") and (item.content or "").strip()
    ]
    # 空列表当作「没带历史」，让调用方回落到进程内记忆
    return history[-MAX_HISTORY_MESSAGES:] or None


async def stream_agent(
    db: Session, current_user: User, data: ChatRequest
) -> AsyncIterator[dict]:
    """流式跑完整条工作流，逐条 yield 事件供 SSE 下发。

    事件类型（前端 Agent Trace Panel 依赖这套契约）：
      session    会话已建立，回传 conversation_id
      node       某个节点开始/结束（含耗时），驱动轨迹面板的进度
      analysis   需求理解结果（已解析槽位、缺失项、是否授权）
      plan       任务计划（步骤列表）
      step       单步工具执行的状态流转
      reflection 反思判定（finish / replan）
      token      最终回复的流式文本
      done       本轮结束，带完整答案
      error      出错，带可读的中文提示
    """
    try:
        query = _last_user_query(data)
    except BusinessException as exc:
        yield {"type": "error", "message": exc.message}
        return

    conversation_id = ensure_conversation_id(data)
    yield {"type": "session", "conversation_id": conversation_id}
    yield {"type": "status", "message": "正在理解您的需求…"}

    history = _history_from_request(data, query)
    runner = workflow.WorkflowRunner(
        db, current_user, conversation_id, query, history=history
    )
    try:
        async for event in runner.astream():
            yield event
    except BusinessException as exc:
        yield {"type": "error", "message": exc.message}
    except Exception:  # noqa: BLE001
        logger.exception("Agent 工作流执行失败")
        yield {"type": "error", "message": "Agent 执行失败，请稍后重试"}


def run_agent(db: Session, current_user: User, data: ChatRequest) -> str:
    """非流式入口。返回值保证非空 —— 改造前这里会静默返回 None。"""
    query = _last_user_query(data)
    conversation_id = ensure_conversation_id(data)

    history = _history_from_request(data, query)
    runner = workflow.WorkflowRunner(
        db, current_user, conversation_id, query, history=history
    )
    try:
        answer = asyncio.run(runner.ainvoke())
    except BusinessException:
        raise
    except RuntimeError:
        # 当前线程已有事件循环（被 async 上下文调用时），另起一个跑
        loop = asyncio.new_event_loop()
        try:
            answer = loop.run_until_complete(runner.ainvoke())
        finally:
            loop.close()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent 工作流执行失败")
        raise BusinessException(message="Agent 执行失败，请稍后重试") from exc

    if not (answer or "").strip():
        raise BusinessException(message="Agent 没有返回任何内容，请换一种说法再试")
    return answer.strip()
