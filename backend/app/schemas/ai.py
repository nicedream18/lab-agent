from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: str
    # 仅回复时下发，用于前端按会话拉取 Agent 执行轨迹
    conversation_id: str | None = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    # 前端首次对话可不传，后端会生成一个并随流式事件回传，
    # 后续轮次带上它，短期记忆与轨迹才能串成同一会话。
    conversation_id: str | None = None
