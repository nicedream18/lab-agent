"""Agent 可观测性 API：执行轨迹、工具箱、长期记忆。

这三个接口是「Agent 不是黑盒」的证据链：
  /trace    把每个节点的输入输出、耗时、成败落库后可回放
  /tools    把 Agent 真正能碰的业务能力摊开给用户看
  /memory   长期偏好对用户可见、可增删（记忆必须可控，否则就是隐私事故）
"""

from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session

from app.agent import memory as agent_memory
from app.agent import tracer
from app.agent.tools import TOOL_REGISTRY
from app.common.exceptions import BusinessException
from app.common.response import Response
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.user_memory import UserMemory
from app.schemas.agent import (
    AgentTraceItem,
    AgentTraceResponse,
    ToolMeta,
    UserMemoryCreateRequest,
    UserMemoryItem,
)

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


@router.get("/tools")
def list_tools(current_user: User = Depends(get_current_user)):
    """列出 Agent 可调用的全部业务工具（前端「工具箱」面板用）。"""
    tools = [
        ToolMeta(name=tool.name, label=tool.label, description=tool.description)
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
