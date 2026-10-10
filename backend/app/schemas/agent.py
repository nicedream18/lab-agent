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


class AgentToolExecutionItem(BaseModel):
    """一次工具调用的审计记录。

    与 AgentTraceItem 的关键区别：tool_input / tool_output 是**可解析的 JSON 文本**，
    不是给人读的散文。前端想看细节可以自己 json.parse，
    想看摘要就看 observation —— 两个字段分开存就是为了两种用法都不别扭。
    """

    id: int
    conversation_id: str
    step_index: int
    tool_name: str
    tool_label: str | None = None
    tool_input: str | None = None
    tool_output: str | None = None
    observation: str | None = None
    is_write: int = 0
    status: str = "success"
    execution_time: float = 0.0
    error: str | None = None
    create_time: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class AgentToolExecutionResponse(BaseModel):
    conversation_id: str
    total: int
    total_time: float = 0.0
    # 其中有多少次是写库操作。单独给一个数，是因为「Agent 到底动了几次数据」
    # 是审计时第一眼要看的量，不该让调用方自己去遍历 items 数。
    write_total: int = 0
    failed_total: int = 0
    items: list[AgentToolExecutionItem] = []


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
    # 是不是写库工具。前端据此给写库工具加标记 ——
    # 一个「能改你数据」的工具和一个「只读」的工具，
    # 在界面上长得一样是不负责任的。
    is_write: bool = False


class AgentConversationItem(BaseModel):
    """会话列表里的一行（侧栏「历史会话」用）。

    刻意不带 messages：列表只需要能认出「这是哪次对话」，
    把正文一起拖出来会让侧栏的查询变成一次全表大字段扫描。
    """

    conversation_id: str
    title: str = ""
    message_count: int = 0
    last_time: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class AgentConversationMessage(BaseModel):
    """会话里的一条消息。"""

    role: str
    content: str
    create_time: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class AgentConversationDetail(BaseModel):
    """一条会话 + 它的全部消息（点开某条历史对话时拉取）。"""

    conversation_id: str
    title: str = ""
    message_count: int = 0
    last_time: datetime | None = None
    messages: list[AgentConversationMessage] = []
