from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AiMessage(Base):
    """AI 会话里的一条消息（用户提问或助手回答）。

    和设备/实验室那类实体不同，这里存的是**聊天原文**，所以：
      - content 用 Text 而不是 String(N)：模型的回答动辄一两千字，
        按 MySQL 的字符串长度限制会直接报 `Data too long`；
      - 不做软删除、不加业务状态：保留期内就是有，出了保留期就是没有。

    一条 `user` 紧跟一条 `assistant` 成对写入（见 conversation_service.record_turn）：
    我们只落**已经答完**的轮次，所以不存在「只有提问没有回答」的悬空记录 ——
    那种半截数据铺回界面上只会让用户困惑。
    """

    __tablename__ = "ai_message"
    __table_args__ = {"comment": "AI 聊天消息"}

    user_id: Mapped[int] = mapped_column(Integer, index=True, comment="所属用户")
    conversation_id: Mapped[str] = mapped_column(
        String(64), index=True, comment="会话ID"
    )
    role: Mapped[str] = mapped_column(String(16), comment="角色：user/assistant")
    content: Mapped[str] = mapped_column(Text, comment="消息正文")
