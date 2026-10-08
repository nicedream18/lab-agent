"""Agent 工作流的状态定义。

LangGraph 的 state 是节点之间**唯一**的数据载体：
  每个节点收到完整 state，只返回「需要更新的字段」的增量字典。

这里刻意用 TypedDict 而不是 dataclass —— LangGraph 对 TypedDict 的支持最完整，
且每个字段都是「最后一个写入者获胜」的默认 channel 语义，行为可预测。
"""

from dataclasses import dataclass
from typing import Any, Callable, TypedDict

# 一次问答允许执行的最大工具步数。
# 没有这个上限，模型可能规划出 20 步的「计划」，把 5 秒的接口拖成 60 秒。
MAX_PLAN_STEPS = 6

# 反思后最多重新规划几轮。
# 2 轮是经验值：第 1 轮往往是「参数不对」，第 2 轮是「换个时段」；
# 到第 3 轮基本就是在空转了（比如实验室确实已经关闭）。
MAX_REPLAN_ROUNDS = 2

# 短期记忆保留的最近消息条数
MAX_HISTORY_MESSAGES = 20

# 节点 → 前端展示文案。放在这里而不是 prompts.py，
# 因为它是「控制流元信息」，tracer 和 graph 都要用。
NODE_LABELS = {
    "analyze": "理解用户需求",
    "plan": "制定任务计划",
    "route": "选择工具",
    "execute": "执行工具",
    "reflect": "反思与校验",
    "respond": "生成最终回复",
}


class PlanStep(TypedDict, total=False):
    """计划里的一步。

    tool 必须是 tools.TOOL_REGISTRY 里注册过的名字 —— planner 产出的计划
    会在进入执行前被白名单过滤，模型幻觉出来的工具名不会被执行。
    """

    id: int
    tool: str
    reason: str  # 为什么要做这一步，会展示在前端 Trace Panel 上
    args: dict[str, Any]
    status: str  # pending / running / done / failed / skipped


class AgentState(TypedDict, total=False):
    """工作流全局状态。"""

    # ---------- 身份与会话 ----------
    user_id: int
    conversation_id: str
    today: str  # 服务器当天日期，注入提示词用于「明天/后天」换算

    # ---------- 理解阶段产出 ----------
    user_query: str
    intent: str  # reserve_lab / query_lab / query_rules / query_equipment / other
    slots: dict[str, Any]  # lab_name / date / start_time / end_time / ...
    missing_slots: list[str]  # 缺失的关键信息，用于向用户追问
    # 预约授权闸门：只有它为 True，计划里才允许出现 create_reservation。
    # 这是防止「Agent 自作主张写库」的最后一道保险。
    authorized: bool
    history: list[dict]  # 短期记忆里的对话上下文

    # ---------- 规划阶段产出 ----------
    plan: list[PlanStep]
    cursor: int  # 当前执行到第几步（下标）
    replan_round: int
    # 只读轮次：目标时段被占用后，系统会强制再跑一轮「只查替代时段、
    # 不再写库」的规划。这个标记就是在表达“本轮不允许出现写库工具”。
    read_only: bool

    # ---------- 执行阶段产出 ----------
    tool_results: dict[str, Any]  # 工具名 → 结构化结果
    observations: list[str]  # 工具结果的自然语言摘要，喂给规划和反思
    halt: bool  # 出现致命错误，跳过剩余步骤直接去反思

    # ---------- 反思阶段产出 ----------
    reflection: str  # 反思结论（自然语言）
    verdict: str  # continue / replan / finish

    # ---------- 输出 ----------
    final_response: str

    # ---------- 旁路 ----------
    memory_context: str  # 长期记忆拼成的一段文本
    error: str


@dataclass
class AgentContext:
    """节点运行时依赖（DB 会话、当前用户、流式写入器）。

    为什么不塞进 AgentState？因为 State 会被 LangGraph 序列化/合并，
    而 Session 和 User 是「活对象」，塞进去既不可序列化也容易出意外。
    用闭包 + dataclass 传递更干净。
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
