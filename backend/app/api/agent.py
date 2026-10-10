"""Agent 可观测性 API：执行轨迹、工具调用审计、工具箱、长期记忆、聊天记录。

这几个接口是「Agent 不是黑盒」的证据链：
  /trace            把每个节点的输入输出、耗时、成败落库后可回放
  /tool-executions  把每一次**工具调用**的入参、结果、耗时、成败摊开（审计）
  /tools            把 Agent 真正能碰的业务能力摊开给用户看
  /memory           长期偏好对用户可见、可增删（记忆必须可控，否则就是隐私事故）
  /conversations    最近 30 天的历史会话（可列出、可回看、可续聊）
"""

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.agent import memory as agent_memory
from app.agent import tracer
from app.agent.state import WRITE_TOOLS
from app.agent.tools import TOOL_REGISTRY, tool_label
from app.common.exceptions import BusinessException
from app.common.response import Response
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.user_memory import UserMemory
from app.schemas.agent import (
    AgentConversationDetail,
    AgentConversationItem,
    AgentConversationMessage,
    AgentToolExecutionItem,
    AgentToolExecutionResponse,
    AgentTraceItem,
    AgentTraceResponse,
    ToolMeta,
    UserMemoryCreateRequest,
    UserMemoryItem,
)
from app.services import conversation_service

router = APIRouter(prefix="/agent", tags=["Agent工作流"])


@router.get("/trace/{conversation_id}")
def get_trace(
    conversation_id: str = Path(..., max_length=64),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """回放某个会话的完整执行轨迹。

    只返回当前用户自己的轨迹 —— conversation_id 虽然是随机的，
    但也不能仅凭猜中 ID 就允许读别人的执行内容。
    """
    rows = [
        row
        for row in tracer.load_trace(db, conversation_id)
        if row.user_id == current_user.id
    ]
    total_time = round(sum(row.execution_time or 0.0 for row in rows), 3)
    return Response.success(
        data=AgentTraceResponse(
            conversation_id=conversation_id,
            total=len(rows),
            total_time=total_time,
            items=[AgentTraceItem.model_validate(row) for row in rows],
        )
    )


@router.get("/tool-executions/{conversation_id}")
def get_tool_executions(
    conversation_id: str = Path(..., max_length=64),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """回放某个会话里 Agent 调过的**每一个工具**（入参、结果、耗时、成败）。

    与 /trace 的分工：
      /trace             回答「Agent 每一步在想什么」（节点粒度）
      /tool-executions   回答「Agent 对外实际做了什么」（工具粒度，可审计）

    同样只返回当前用户自己的记录 —— 工具的入参里可能带实验室、时间、
    预约编号等信息，不能仅凭猜中 conversation_id 就读别人的。
    """
    rows = [
        row
        for row in tracer.load_tool_executions(db, conversation_id)
        if row.user_id == current_user.id
    ]
    total_time = round(sum(row.execution_time or 0.0 for row in rows), 3)
    return Response.success(
        data=AgentToolExecutionResponse(
            conversation_id=conversation_id,
            total=len(rows),
            total_time=total_time,
            write_total=sum(1 for row in rows if row.is_write),
            failed_total=sum(1 for row in rows if row.status == "failed"),
            items=[AgentToolExecutionItem.model_validate(row) for row in rows],
        )
    )


@router.get("/tools")
def list_tools(_: User = Depends(get_current_user)):  # 参数仅为触发鉴权
    """列出 Agent 可调用的全部业务工具（前端「工具箱」面板用）。"""
    tools = [
        ToolMeta(
            name=tool.name,
            label=tool_label(tool.name),
            description=tool.description,
            is_write=tool.name in WRITE_TOOLS,
        )
        for tool in TOOL_REGISTRY.values()
    ]
    return Response.success(data=tools)


@router.get("/memory")
def list_memory(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """当前用户的长期记忆。"""
    rows = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == current_user.id)
        .order_by(UserMemory.hit_count.desc(), UserMemory.id.desc())
        .limit(50)
        .all()
    )
    return Response.success(data=[UserMemoryItem.model_validate(row) for row in rows])


@router.post("/memory")
def create_memory(
    data: UserMemoryCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """手动新增一条长期记忆（也可用于演示「越用越懂我」）。"""
    row = agent_memory.remember(
        db, current_user.id, data.content, memory_type=data.memory_type
    )
    if row is None:
        raise BusinessException(message="记忆写入失败，请稍后重试")
    return Response.success(data=UserMemoryItem.model_validate(row), message="已记住")


@router.delete("/memory/{memory_id}")
def delete_memory(
    memory_id: int = Path(..., ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除自己的某条长期记忆。"""
    row = (
        db.query(UserMemory)
        .filter(UserMemory.id == memory_id, UserMemory.user_id == current_user.id)
        .first()
    )
    if row is None:
        raise BusinessException(message="记忆不存在", code=404)
    db.delete(row)
    db.commit()
    return Response.success(message="已忘记")


# ---------------------------------------------------------------------------
# 历史会话（聊天记录）
#
# 保留最近 30 天，过期自动清理（见 services/conversation_service）。
# 三个接口都强制带 user_id 过滤 —— conversation_id 虽然是随机的，
# 但也不能仅凭猜中 ID 就允许读别人的聊天原文。
# ---------------------------------------------------------------------------


@router.get("/conversations")
def list_conversations(
    limit: int = Query(conversation_service.DEFAULT_LIST_LIMIT, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """最近一个月的历史会话，按最后活跃时间倒序。"""
    rows = conversation_service.list_conversations(db, current_user.id, limit)
    return Response.success(
        data=[AgentConversationItem.model_validate(row) for row in rows]
    )


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str = Path(..., max_length=64),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取回一条历史会话的全部消息，用于「选中它接着聊」。"""
    found = conversation_service.load_conversation(db, current_user.id, conversation_id)
    if found is None:
        # 措辞统一成「不存在或已清理」：既覆盖越权访问，也覆盖超过保留期被清掉，
        # 同时不泄露「这个 ID 到底存不存在」。
        raise BusinessException(message="该会话不存在或已超过保留期", code=404)

    conversation, messages = found
    return Response.success(
        data=AgentConversationDetail(
            conversation_id=conversation.conversation_id,
            title=conversation.title or "",
            message_count=conversation.message_count or 0,
            last_time=conversation.last_time,
            messages=[
                AgentConversationMessage.model_validate(message) for message in messages
            ],
        )
    )


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str = Path(..., max_length=64),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除自己的一条历史会话（连同消息）。

    刻意不报「找不到」：删除是幂等的，用户点两次就该看到同样的结果。
    """
    removed = conversation_service.delete_conversation(
        db, current_user.id, conversation_id
    )
    return Response.success(data={"deleted": removed}, message="已删除")
