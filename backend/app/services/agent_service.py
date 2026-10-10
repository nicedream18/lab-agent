"""Agent 门面（Facade）。

为什么还需要这一层？
API 层不应该知道 LangGraph 的存在 —— 它只关心「给我一句话，我还你一串事件」。
把会话 ID 生成、请求校验、异常兜底这些 HTTP 周边的琐事收在这里，
`app/agent/graph.py` 就能保持纯粹的「工作流」语义。

改造说明（v2，native tool calling）：
本文件只做门面 —— 会话 ID、请求校验、异常兜底、把工作流的事件转出去。
它不关心工作流长什么样，也不关心模型是自己挑工具还是按计划走，
所以从显式工作流改成原生 function calling 时，这里只换了几行注释。
当前工作流是 `analyze → agent ⇄ execute → respond`（见 `app/agent/graph.py`）：
模型通过 tool_calls 自主决定调哪个工具、带什么参数。

v3 起它还多担一件事：把跑完的一轮问答写进**聊天记录**（conversation_service）。
放在这一层而不是 WorkflowRunner 里，是因为「用户的聊天记录」是产品级概念，
不是工作流概念 —— 换成别的编排方式时，这段逻辑不该跟着搬家。
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
from app.services import conversation_service

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


def _record_turn(
    db: Session, current_user: User, conversation_id: str, question: str, answer: str
) -> None:
    """把答完的一轮写进聊天记录（供「历史会话」列表回看、接着聊）。

    为什么只落**已经答完**的轮次：半截记录（有问无答）铺回界面上只会让人困惑，
    而用户真正想翻的是「上次聊到哪、结论是什么」。

    会话 id 用的是 `ensure_conversation_id` 生成的那个，和 agent_trace /
    agent_tool_execution 完全一致 —— 前端拿到它就能把三份数据对上。
    """
    if not (answer or "").strip():
        return
    conversation_service.record_turn(
        db, current_user.id, conversation_id, question, answer.strip()
    )


async def stream_agent(
    db: Session, current_user: User, data: ChatRequest
) -> AsyncIterator[dict]:
    """流式跑完整条工作流，逐条 yield 事件供 SSE 下发。

    事件类型（前端 Agent Trace Panel 依赖这套契约）：
      session    会话已建立，回传 conversation_id
      node       某个节点开始/结束（含耗时），驱动轨迹面板的进度
      analysis   需求理解结果（已解析槽位、缺失项、是否授权）
      step       模型点名的工具开始执行 / 执行完毕（含参数、结果、耗时）
      reset      本轮已显示的文字作废（模型先说了句铺垫又去调工具，那段不算答案）
      token      最终回复的文本
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
    # 把最终回答从事件流里捞出来，等整轮跑完再落库。
    # 为什么不用 token 事件拼接：那一串只是「已吐给前端的碎片」，
    # done 事件里的 answer 才是这一轮的权威答案。
    answer = ""
    try:
        async for event in runner.astream():
            if event.get("type") == "done":
                answer = event.get("answer") or ""
            yield event
    except BusinessException as exc:
        yield {"type": "error", "message": exc.message}
    except Exception:
        logger.exception("Agent 工作流执行失败")
        yield {"type": "error", "message": "Agent 执行失败，请稍后重试"}
    else:
        # 只有整轮正常跑完且拿到回答才落库（上面的 except 分支会让 else 不执行）。
        # 写失败只记日志，不影响用户已经看到的回答 —— 见 conversation_service。
        _record_turn(db, current_user, conversation_id, query, answer)


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
    except Exception as exc:
        logger.exception("Agent 工作流执行失败")
        raise BusinessException(message="Agent 执行失败，请稍后重试") from exc

    if not (answer or "").strip():
        raise BusinessException(message="Agent 没有返回任何内容，请换一种说法再试")
    _record_turn(db, current_user, conversation_id, query, answer)
    return answer.strip()
