from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AiConversation(Base):
    """一条 AI 会话的「台账」：列表页要用的字段全在这里。

    为什么不直接对 ai_message 做 GROUP BY 现算：
      1. 列表要按最后活跃时间倒排、要显示标题和条数，这三样都是聚合结果，
         现算意味着每次打开侧栏都扫一遍消息表（还带着 Text 大字段）；
      2. 会话是有「开始」和「结束」的独立实体 —— 用户点了「清空对话」就是结束了
         一次会话，这个动作需要有地方记录，而不只是把消息删掉。

    所以这里存索引信息，ai_message 存正文，两边靠 conversation_id 关联。

    保留期 30 天（见 services/conversation_service.RETENTION_DAYS）：
    过期的会话会被**整条物理删除**。这些是聊天原文，留久了只是隐私负债；
    真正有审计价值的 agent_trace / agent_tool_execution 不受影响，不在这条清理链上。
    """

    __tablename__ = "ai_conversation"
    __table_args__ = {"comment": "AI 会话台账（聊天记录索引）"}

    # 刻意不建外键，和 agent_trace 保持一致：这是「记录」而不是业务实体，
    # 建了外键反而会挡住删用户这类操作（项目里没有配级联）。
    user_id: Mapped[int] = mapped_column(Integer, index=True, comment="所属用户")
    # 与 agent_trace / agent_tool_execution 共用同一个会话ID，
    # 前端拿到它对上「聊天记录」「执行轨迹」「工具审计」三份数据
    conversation_id: Mapped[str] = mapped_column(
        String(64), unique=True, comment="会话ID"
    )
    title: Mapped[str] = mapped_column(
        String(120), default="", comment="会话标题，取首轮提问的前若干字"
    )
    message_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="已落库的消息条数（一问一答算 2 条）"
    )
    last_time: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        index=True,
        comment="最后一次对话时间，列表排序与保留期判定都用它",
    )
