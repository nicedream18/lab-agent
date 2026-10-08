from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AgentTraceItem(BaseModel):
    """一条节点执行轨迹，前端 Trace Panel 直接渲染。"""

    id: int
    conversation_id: str
    node_name: str
    step_index: int
    status: str
    input: str | None = None
    output: str | None = None
    execution_time: float = 0.0
    create_time: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class AgentTraceResponse(BaseModel):
    conversation_id: str
    total: int
    total_time: float = 0.0
    items: list[AgentTraceItem] = []


class UserMemoryItem(BaseModel):
    id: int
    memory_type: str
    content: str
    hit_count: int = 0
    create_time: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class UserMemoryCreateRequest(BaseModel):
    memory_type: str = Field(default="preference", max_length=32)
    content: str = Field(min_length=1, max_length=500)


class ToolMeta(BaseModel):
    """工具元信息，用于前端展示「Agent 工具箱」。"""

    name: str
    label: str
    description: str
