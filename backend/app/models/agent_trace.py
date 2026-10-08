from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentTrace(Base):
    """Agent 工作流执行轨迹。

    每跑一个节点写一行，前端据此还原「理解需求 → 制定计划 → 调工具 → 反思 → 回复」
    的完整链路。之所以要落库而不是只走 SSE：
    SSE 是「阅后即焚」的，刷新页面就没了；而 Agent 的可观测性是它区别于
    普通 Chatbot 的核心卖点之一，必须能回看、能复盘。
    """

    __tablename__ = "agent_trace"
    __table_args__ = {"comment": "Agent 工作流执行轨迹"}

    conversation_id: Mapped[str] = mapped_column(
        String(64), index=True, comment="会话ID，同一轮对话共享"
    )
    user_id: Mapped[int] = mapped_column(Integer, index=True, comment="发起用户")
    node_name: Mapped[str] = mapped_column(
        String(32), comment="节点名：analyze/plan/route/execute/reflect/respond"
    )
    step_index: Mapped[int] = mapped_column(
        Integer, default=0, comment="节点在同一次运行中的序号，用于排序"
    )
    status: Mapped[str] = mapped_column(
        String(16), default="success", comment="状态：success/failed/skipped"
    )
    # 输入输出统一存字符串（JSON 文本或自然语言），方便直接展示
    # 不用 JSON 类型是为了兼容 MySQL 5.7 且避免方言差异
    input: Mapped[str | None] = mapped_column(Text, comment="节点输入")
    output: Mapped[str | None] = mapped_column(Text, comment="节点输出")
    execution_time: Mapped[float] = mapped_column(
        Float, default=0.0, comment="耗时（毫秒）"
    )
