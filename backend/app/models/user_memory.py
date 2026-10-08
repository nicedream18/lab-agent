from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserMemory(Base):
    """Agent 的长期记忆：保存用户偏好，跨会话生效。

    短期记忆（当前对话上下文）走内存里的 ConversationMemory，
    这里只存「值得跨会话记住」的结论，例如：
    - preference: 用户偏好光学实验室
    - preference: 用户习惯约下午 14:00-17:00
    - fact:       用户是物理学院研究生

    memory_type 目前有三种，用字符串而不是枚举，方便后续扩展：
    preference / habit / fact
    """

    __tablename__ = "user_memory"
    __table_args__ = {"comment": "Agent 长期记忆：用户偏好"}

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), index=True, comment="所属用户"
    )
    memory_type: Mapped[str] = mapped_column(
        String(32), default="preference", comment="类型：preference/habit/fact"
    )
    content: Mapped[str] = mapped_column(
        String(500), comment="记忆内容，一句话描述"
    )
    hit_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="被注入提示词的次数，可用于淘汰冷记忆"
    )
