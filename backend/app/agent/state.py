"""Agent 工作流的状态定义。

LangGraph 的 state 是节点之间**唯一**的数据载体：
  每个节点收到完整 state，只返回「需要更新的字段」的增量字典。

这里刻意用 TypedDict 而不是 dataclass —— LangGraph 对 TypedDict 的支持最完整，
且每个字段都是「最后一个写入者获胜」的默认 channel 语义，行为可预测。
**唯一的例外是 messages**：它必须用 add_messages reducer 追加而不是覆盖，
因为一轮对话里会有多轮「模型发言 → 工具返回」，见下面的字段说明。

控制流由模型直接返回的 tool_calls 驱动，所以状态里只留「消息流」这一个
滚动载体：模型每一轮的发言和每一次工具返回都追加在 messages 上。

v3 追加了「任务计划」这一层（Plan-and-Execute，条件触发）：只有 analyze
判定 plan_needed=True 时才会写入 plan / plan_cursor / plan_revision 等字段，
快路径（plan_needed=False）下这些字段全程保持初始值，拓扑与行为不变。
它们不是「消息流」的替代品，而是给 agent 节点提供「本步该做什么、
建议用哪些工具」的逐步约束 —— 详见下面的字段说明。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

# 一轮对话里最多允许模型发起几轮工具调用。
#
# 为什么还需要它：native tool calling 把「什么时候停」交给了模型，
# 但模型没有天然的刹车 —— 它可能反复查同一个实验室。这个上限是兜底：
# 到达上限后 graph 会带上 tool_choice="none" 再问一次，
# 逼它基于已有观察结果给出最终回复，而不是直接掐断对话。
MAX_TOOL_ROUNDS = 6

# 短期记忆保留的最近消息条数
MAX_HISTORY_MESSAGES = 20

# ---------------------------------------------------------------------------
# Plan-and-Execute（v3）相关上限。
#
# 触发是**条件式**的：analyze 用同一次 LLM 调用顺带判定 plan_needed，
# 只有 True 才进 plan 节点。单查询 / 闲聊走原来的 agent ⇄ execute 快路径，
# 拓扑与行为零变化 —— 规划层不该为「查一下明天有哪些实验室开放」
# 这种一句话能答的问题付代价。
# ---------------------------------------------------------------------------

# 一份计划最多几步。步骤越多，模型对齐越难，多步共享的工具预算也越紧张。
MAX_PLAN_STEPS = 5

# 允许重规划的最大次数。
#
# 为什么必须有上限：重规划是唯一会「推翻已经跑对的部分」的环节。
# v1 的教训（见 docs/agent-architecture.md §7.6）就是「步骤全成功后还重规划，
# 把已经答对的问题改错了」。这里除了次数上限，还有 graph._replan_needed
# 那道确定性闸门把关 —— 上限只是最后一道保险。
MAX_REPLAN_ROUNDS = 2

# 单个计划步骤内部的工具轮数上限。
#
# 为什么要在全局 MAX_TOOL_ROUNDS 之外再设一个：多步计划共享全局预算，
# 若某一步陷入「反复查同一个实验室」，它会吃光全局 6 轮，后面的步骤
# 一步都拿不到工具。这个上限保证每一步都有独立的、有保证的工具预算。
STEP_TOOL_ROUNDS = 3

# 会改变数据的工具。**这是全系统唯一一份写库工具清单**。
#
# 为什么要集中定义而不是在 planner / graph 里各自写死一个工具名：
# 授权闸门（authorized）和只读闸门（read_only）必须对所有写库工具一视同仁。
# 早先这两处都硬编码了 "create_reservation" —— 那时只有它一个写工具，
# 看不出问题；一旦加入 cancel_reservation，任何漏改的一处都会变成
# 「不受授权闸门约束的写库路径」。把清单收到状态定义旁边，
# 新增写工具时只需要改这一行，并且一眼能看出它涉及哪些控制流。
#
# 判断标准很简单：**这个工具会不会让数据库的内容发生用户可见的变化。**
# 查询类工具无论调用多少次，结果都只是「读了什么」，不在清单里。
#
# 改造后的三个消费点：
#   1. tools.bindable_tools() —— 未授权时把它整个从 bind_tools 的清单里摘掉，
#      模型在**物理上**无法调用（见 prompts.AGENT_SYSTEM_PROMPT 的工具范围段）；
#   2. graph.node_execute 的运行时兜底拦截；
#   3. tracer.write_tool_execution 的 is_write 落库标记。
WRITE_TOOLS = frozenset({"create_reservation", "cancel_reservation"})

# 节点 → 前端展示文案。放在这里而不是 prompts.py，
# 因为它是「控制流元信息」，tracer 和 graph 都要用。
# 前端 src/utils/agentTrace.js 有一份同构的 NODE_LABELS，改这里时记得同步。
NODE_LABELS = {
    "analyze": "理解用户需求",
    "plan": "拆解任务计划",
    "agent": "自主决策",
    "execute": "执行工具",
    "replan": "调整任务计划",
    "respond": "生成最终回复",
}
# 注意：advance 不在表里。它是纯管道节点（只推进游标、不调模型），
# 刻意不产生 node 事件、也不写 agent_trace —— 轨迹面板上多一格
# 「推进到下一步」只会稀释真正有信息量的那几格。


class AgentState(TypedDict, total=False):
    """工作流全局状态。字段按「谁写它」分组，一眼能看出数据的来源和生命周期。"""

    # ---------- 身份与会话（graph 写入，全程只读）----------
    user_id: int
    conversation_id: str
    today: str  # 服务器当天日期，注入提示词用于「明天/后天」换算
    user_query: str
    history: list[dict]  # 短期记忆：前端带上来的最近若干条对话

    # ---------- 需求理解产出（node_analyze 写入）----------
    slots: dict[str, Any]  # lab_name / date / start_time / end_time / equipment_name
    missing_slots: list[str]  # 缺失的关键信息，注入系统提示词
    # 写库授权闸门：只有它为 True，WRITE_TOOLS 才会被放进 bind_tools 的工具清单。
    # 这是防止「Agent 自作主张写库」的第一道（也是结构性的一道）保险 ——
    # 模型不是「被劝阻不要写」，而是「手里根本没有写库工具」。
    authorized: bool

    # ---------- 自主决策（node_agent / node_execute 读写）----------
    # 本轮的模型消息流：AIMessage（可能带 tool_calls）+ 对应的 ToolMessage。
    #
    # 必须用 add_messages reducer，不能靠「最后写入者获胜」：
    # agent 节点追加一条 AIMessage，execute 节点追加 N 条 ToolMessage，
    # 两个节点都要在既有消息**之后**追加，覆盖语义会把前一条冲掉。
    #
    # 这是 native tool calling 的核心收益所在：工具结果以结构化消息留在
    # 上下文里，模型下一轮真的「看得见」上一次查到的预约编号，
    # 而不是靠把结果压成一段文字再喂回去。
    messages: Annotated[list[AnyMessage], add_messages]
    # 已完成的工具调用轮数，用于 MAX_TOOL_ROUNDS 刹车
    tool_round: int
    # 本轮 agent 决策出、等待 execute 执行完的工具调用
    # （元素形如 {"id": ..., "name": ..., "args": {...}}）
    pending_calls: list[dict[str, Any]]
    # 这一轮**实际提供给模型**的工具名清单（node_agent 写入，node_execute 只读）。
    #
    # 为什么需要它：bind_tools 的工具清单是「API 层约束」，而实测部分
    # OpenAI 兼容网关并不严格遵守 —— 模型偶尔会吐出一个不在清单里的工具名。
    # 只靠清单挡不住，所以执行前再核一遍：不在清单里的调用标成失败，
    # 并把「本步只能用哪些」回给模型，让它下一轮自己纠正。
    # 这样 bindable_tools 文档里那句「没提供的工具是它真的拿不到」才成立。
    bound_tools: list[str]

    # ---------- 任务计划（node_analyze → node_plan → node_advance 维护）----------
    # 只有 analyze 判定 plan_needed=True 时才有内容；快路径下保持初始值。
    # 元素形如 {"step": 1, "goal": "...", "hint_tools": [...], "depends_on": [...]}。
    # hint_tools 为空列表表示「本步不限工具」。
    plan: list[dict[str, Any]]
    # 由 analyze 写入的 LLM 判定：这一轮要不要走规划路径。
    # ⚠️ 它必须是模型给出的结论，绝不能在代码里写「有'然后'这个词就规划」——
    # 那是关键词驱动的流程控制，本项目明令禁止。
    plan_needed: bool
    # 当前执行到第几步（0 基下标）。快路径下恒为 0。
    plan_cursor: int
    # 已经重规划过几次，用于 MAX_REPLAN_ROUNDS 上限。
    plan_revision: int
    # 最近一次「为什么这样规划 / 这样调整」的一句话，仅用于轨迹展示。
    plan_reason: str
    # 当前这一步内部已经用掉几轮工具。跨步时由 advance 归零。
    step_round: int
    # 每一步收尾时的快照，形如 {index, goal, ok, calls, note}。
    # replan 靠它知道「哪些已经做成了」，_replan_needed 也靠它判断
    # 「是不是所有步骤都成功」—— 这是 v1 §7.6 教训的结构化落点。
    step_results: list[dict[str, Any]]

    # ---------- 旁路 ----------
    memory_context: str  # 长期记忆拼成的一段文本
    tool_results: list[dict[str, Any]]  # 本轮工具调用审计（{tool,args,ok,result}）
    error: str

    # ---------- 输出 ----------
    final_response: str


@dataclass
class AgentContext:
    """节点运行时依赖（DB 会话、当前用户、流式写入器）。

    为什么不塞进 AgentState？因为 State 会被 LangGraph 序列化/合并，
    而 Session 和 User 是「活对象」，塞进去既不可序列化也容易出意外。
    用闭包 + dataclass 传递更干净。

    这同时也是**为什么工具的 ctx 必须标成注入参数**：handler 的第一个参数
    就是这个 ctx。如果把它当成普通参数，@tool 就会把它暴露成模型可见的
    JSON 字段，让模型去编一个 ctx 出来。所以工具统一写成

        def some_tool(ctx: Annotated[AgentContext, InjectedToolArg], ...): ...

    InjectedToolArg 会把 ctx 从**发给模型的 schema** 里摘掉，
    而 ctx 只在 run_tool() 里由 Python 侧注入（调 tool.func(ctx, **args)）。
    """

    db: Any
    user: Any
    conversation_id: str
    writer: Callable[[dict], None] | None = None
    # 节点执行序号，tracer 用它保证轨迹按真实顺序落库
    counter: int = 0
    # 本轮对话里模型连续失败的次数。
    # 存在 ctx 上而不是模块级变量上：熔断必须是「一轮一问」的，
    # 上一轮被限流不该让下一轮也直接放弃模型。
    llm_failures: int = 0

    def emit(self, event: dict) -> None:
        """向前端推一条流式事件；非流式场景 writer 为 None，静默丢弃。

        必须吞异常：LangGraph 在非流式（invoke/ainvoke）运行时也会调用节点，
        此时 get_stream_writer() 拿到的写入器行为不确定，
        不能因为「推事件失败」把整轮问答搞崩。
        """
        if self.writer is None:
            return
        try:
            self.writer(event)
        except Exception:  # noqa: BLE001
            self.writer = None

    def next_index(self) -> int:
        self.counter += 1
        return self.counter
