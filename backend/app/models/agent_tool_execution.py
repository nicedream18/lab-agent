from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentToolExecution(Base):
    """Agent 每一次**工具调用**的独立记录。

    为什么已经有了 agent_trace 还要再建一张表？
    两者的粒度和受众根本不同，混在一张表里会让两边都用不好：

      agent_trace            节点级 · 「Agent 在想什么」
                             analyze / plan / agent / execute / replan / respond
                             一个 execute 节点可能对应任意一个工具，看不出调了谁

      agent_tool_execution   工具级 · 「Agent 对外做了什么」
                             一次调用一行，带工具名、入参、返回值、耗时、成败

    第二张表回答的是审计问题：这个 Agent 到底碰过哪些业务能力？带什么参数？
    结果是什么？有多慢？—— 这也是「Tool Calling 不是黑盒」的证据链，
    Agent 被授予了写库能力，那它每一次写就必须留下可追溯的一行。

    刻意不复用 agent_trace：那张表的 input/output 是「节点的输入输出」，
    语义上允许是人话摘要；这张表的 tool_input/tool_output 要求是
    **可解析的 JSON**，两者一旦混用，字段含义就会随写入方漂移。
    """

    __tablename__ = "agent_tool_execution"
    __table_args__ = {"comment": "Agent 工具调用记录"}

    conversation_id: Mapped[str] = mapped_column(
        String(64), index=True, comment="会话ID，同一轮对话共享"
    )
    user_id: Mapped[int] = mapped_column(Integer, index=True, comment="发起用户")
    step_index: Mapped[int] = mapped_column(
        Integer, default=0, comment="计划中的第几步（从 1 开始）"
    )
    tool_name: Mapped[str] = mapped_column(
        String(64), index=True, comment="工具名，如 create_reservation"
    )
    tool_label: Mapped[str | None] = mapped_column(
        String(64), comment="工具中文名，冗余一份方便直接展示"
    )
    # 存 JSON 文本而不是 JSON 类型：兼容 MySQL 5.7，且避免方言差异
    tool_input: Mapped[str | None] = mapped_column(
        Text, comment="调用参数（JSON 文本）"
    )
    tool_output: Mapped[str | None] = mapped_column(
        Text, comment="返回结果（JSON 文本）"
    )
    observation: Mapped[str | None] = mapped_column(
        Text, comment="给模型的自然语言摘要，与 tool_output 分开存便于对比"
    )
    # 该工具是不是写库操作。落一份冗余标记，审计时可以一条 SQL 捞出所有写操作，
    # 不用去 join 代码里的 WRITE_TOOLS 常量（那张表会随版本变化）。
    is_write: Mapped[int] = mapped_column(
        Integer, default=0, comment="是否写库操作：0-只读，1-写库"
    )
    status: Mapped[str] = mapped_column(
        String(16), default="success", comment="状态：success/failed/skipped"
    )
    execution_time: Mapped[float] = mapped_column(
        Float, default=0.0, comment="耗时（毫秒）"
    )
    error: Mapped[str | None] = mapped_column(Text, comment="失败原因，成功时为空")
