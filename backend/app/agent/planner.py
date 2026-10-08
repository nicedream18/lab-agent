"""Agent 的 LLM 交互层。

职责边界很明确：**这里只负责「和模型对话 + 解析输出」，不碰数据库、
不决定控制流**。控制流在 graph.py，业务逻辑在 tools.py / services。

为什么每个环节都要有降级兜底？
大模型返回不合规 JSON 是常态而不是异常。如果解析失败就直接把 500 抛给用户，
这个 Agent 的可用性会差到没法演示。所以 analyze/plan 都有启发式兜底路径：
模型不行时，用规则顶上，宁可「笨一点」也要「跑得通」。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Callable

import asyncio

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from app.agent import prompts
from app.agent.state import (
    MAX_PLAN_STEPS,
    MAX_REPLAN_ROUNDS,
    AgentContext,
    AgentState,
    PlanStep,
)
from app.agent.tools import TOOL_REGISTRY, tool_catalog, tool_label
from app.common.exceptions import BusinessException
from app.config import settings
from app.utils.net import prefer_ipv4

logger = logging.getLogger(__name__)

# 让大模型域名走 IPv4（见 app/utils/net.py 里的原因说明）。
# 放在模块级而不是某个函数里：必须早于任何一次异步出网请求生效。
if settings.LLM_FORCE_IPV4:
    prefer_ipv4(settings.LLM_BASE_URL)

# 意图取值集合，和 prompts.ANALYZE_PROMPT 里保持一致
VALID_INTENTS = {
    "reserve_lab",
    "query_lab",
    "query_equipment",
    "query_rules",
    "query_my_reservation",
    "cancel_reservation",
    "other",
}


# ---------------------------------------------------------------------------
# 基础工具函数
# ---------------------------------------------------------------------------


def get_llm(streaming: bool = False) -> ChatOpenAI:
    return ChatOpenAI(
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        model=settings.LLM_MODEL,
        # 结构化任务 temperature 必须为 0：同样的输入要拿到同样的 JSON，
        # 否则规划结果会飘，轨迹也没法复现。
        temperature=0,
        streaming=streaming,
        # 必须显式设超时。SDK 默认 600 秒，供应商卡住时「不报错」比「报错」更糟 ——
        # 用户会以为前端挂了。15 秒等不来的回答，交给规则兜底反而更有价值。
        timeout=settings.LLM_TIMEOUT,
        # 关掉 SDK 自带重试：它会和 planner 的重试循环相乘（2×3=6 次请求），
        # 把最坏等待时间放大到分钟级。重试策略只由 planner 一处掌管，
        # 这样「重试还是兜底」这个决策才可审计、可调。
        max_retries=0,
    )


def message_text(message: Any) -> str:
    """把模型返回的 content 统一成字符串。

    content 有时是 str，有时是 [{"type": "text", "text": ...}] 块列表。
    """
    if message is None:
        return ""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or "")
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return str(content) if content is not None else ""


def chunk_text(chunk: Any) -> str:
    return message_text(chunk)


def load_json(text: str) -> dict:
    """从模型输出里抠出 JSON。

    模型经常不老实：加 ```json 围栏、前面来一句「好的，这是结果：」、
    甚至末尾多打个逗号。所以不能直接 json.loads。
    """
    raw = (text or "").strip()
    # 去掉 markdown 代码围栏
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"模型输出里找不到 JSON：{raw[:200]}")

    candidate = raw[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # 常见脏数据：尾随逗号 {"a":1,}
        fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
        return json.loads(fixed)


def _invoke(messages: list) -> str:
    try:
        response = get_llm().invoke(messages)
    except Exception as exc:  # noqa: BLE001
        logger.exception("调用大模型失败")
        raise BusinessException(message=f"大模型调用失败：{exc}") from exc
    return message_text(response)


# 重试节奏（秒）。首次不等待，之后退避两次。
# 不宜太长：限流窗口往往是分钟级，等 60s 对交互式对话体验是灾难，
# 而真正的兜底是下面的规则降级路径，重试只解决“瞬时抖动”。
RETRY_DELAYS = (0.0, 2.0, 6.0)

# 耗时超过这个阈值才失败的调用，不再重试。
#
# 实测这个供应商的失败有两种截然不同的形态：
#   1. 几毫秒内连接被重置 —— 重试很有价值；
#   2. 请求挂十几秒后才报错 —— 重试只是让用户把同样的等待再受一遍。
# 用「耗时」区分它们比用异常名字区分更贴近真实原因。
SLOW_FAILURE_SECONDS = 8.0


def too_slow_to_retry(started: float) -> bool:
    """这次失败是不是“挂太久”导致的？挂太久就不值得再试。"""
    return time.monotonic() - started > SLOW_FAILURE_SECONDS


# 单轮对话内的模型熔断阈值。
#
# 为什么需要它：实测供应商进入限流窗口后，表现不是「快速拒绝」而是
# **每个请求都挂到超时**。此时如果每个节点都老老实实等一遍超时，
# 一轮对话（analyze/plan/reflect/respond 四个节点）就是几十秒的空白 ——
# 用户看到的是页面卡死，而不是「系统正在降级」。
# 连续失败到阈值就停止向模型要东西，后面的节点直接走确定性路径，
# 把最坏耗时压到「一次超时」，同时保证业务流程仍然给得出结论。
#
# 阈值取 1 是划算的：计数的粒度是「一次 _ask 彻底放弃」，
# 而 _ask 内部已经把瞬时抖动重试掉了。真正走到这里，说明模型确实不可用；
# 而此时代价很低 —— 下面每个环节都有确定性兜底，主流程照样跑得完。
BREAKER_THRESHOLD = 1


def breaker_open(ctx: AgentContext) -> bool:
    """模型连续失败已达阈值：本轮不再向它要东西。"""
    return ctx.llm_failures >= BREAKER_THRESHOLD


def _record(ctx: AgentContext, ok: bool) -> None:
    """记一次模型调用结果，维护熔断计数。

    只在「一次完整的 _ask 终于成功 / 彻底放弃」时调用 ——
    单次尝试的失败不该记账，否则一次瞬时抖动就能把熔断打开。
    """
    if ok:
        ctx.llm_failures = 0
    else:
        ctx.llm_failures += 1

_TRANSIENT_ERRORS = (
    "OpenAIRateLimitError",
    "OpenAITimeoutError",
    "OpenAIConnectionError",
    "APITimeoutError",
    "APIConnectionError",
    "InternalServerError",
)


def is_rate_limited(exc: Exception) -> bool:
    """是不是配额/限流信号（429）。

    必须和「网络抖动」分开对待。实测免费额度下的 429 长这样：
        {'code': '', 'message': '您已达到免费用户的 API 速率限制。…'}
    它不是「稍等一下网络就好了」，而是「你的额度用完了」。
    对它的正确反应是**减少请求**，而不是重试 ——
    重试只会更快烧掉配额，还会把用户晾在退避里。
    """
    if type(exc).__name__ in ("OpenAIRateLimitError", "RateLimitError"):
        return True
    return "429" in str(exc) or "速率限制" in str(exc)


def is_transient(exc: Exception) -> bool:
    """判断是不是「等一下再来就好」的网络类抖动。

    注意：429 不算 transient（见 is_rate_limited）——
    把限流当抖动重试，是这个项目里最容易犯、也最伤配额的错误。
    网关 5xx 和连接被重置才是真正值得再试一次的形态。
    """
    if type(exc).__name__ in _TRANSIENT_ERRORS:
        return not is_rate_limited(exc)
    if is_rate_limited(exc):
        return False
    return any(
        f" {code}" in str(exc) or f"code: {code}" in str(exc)
        for code in (500, 502, 503, 504)
    )


def _ask(ctx: AgentContext, prompt: str) -> str:
    """所有结构化环节都走这一个入口，便于统一加缓存 / 重试 / 埋点。

    熔断计数只在这里记：内部重试属于「这一问还没问完」，
    不该对外表现出连续失败。
    """
    last: Exception | None = None
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        if delay:
            time.sleep(delay)
        started = time.monotonic()
        try:
            text = _invoke([HumanMessage(content=prompt)])
        except BusinessException as exc:
            cause = exc.__cause__ or exc
            if not is_transient(cause) or too_slow_to_retry(started):
                _record(ctx, ok=False)
                if is_rate_limited(cause):
                    # 明确记一笔：熔断打开后本轮不再消耗配额。
                    logger.warning("模型配额已用尽（429），本轮其余环节改走确定性路径")
                raise
            last = exc
            logger.warning("模型调用第 %s 次失败，稍后重试：%s", attempt, cause)
        else:
            _record(ctx, ok=True)
            return text
    _record(ctx, ok=False)
    raise last  # type: ignore[misc]


def _history_text(history: list[dict] | None, limit: int = 6) -> str:
    if not history:
        return "（无历史对话）"
    recent = history[-limit:]
    lines = []
    for item in recent:
        role = "用户" if item.get("role") == "user" else "助手"
        content = (item.get("content") or "").strip().replace("\n", " ")
        if len(content) > 200:
            content = content[:200] + "…"
        lines.append(f"{role}：{content}")
    return "\n".join(lines)


def _slots_text(slots: dict) -> str:
    filled = {k: v for k, v in (slots or {}).items() if v not in (None, "", [])}
    if not filled:
        return "（未提取到任何信息）"
    return json.dumps(filled, ensure_ascii=False)


def _find_tool_name(step: PlanStep) -> str:
    return (step.get("tool") or "").strip()


def required_args(tool_name: str) -> list[str]:
    tool = TOOL_REGISTRY.get(tool_name)
    if not tool:
        return []
    return list(tool.parameters.get("required", []))


def args_complete(step: PlanStep) -> bool:
    """计划里这一步的参数是否已经齐了。

    齐了就跳过 LLM 路由，直接执行 —— 省一次模型调用，
    也让「计划已经写清楚」的步骤行为完全确定。
    """
    args = step.get("args") or {}
    for key in required_args(_find_tool_name(step)):
        value = args.get(key)
        if value in (None, "", []):
            return False
    return True


# ---------------------------------------------------------------------------
# 节点 1：需求理解
# ---------------------------------------------------------------------------

# 用户「字面上下达预约指令」的确定性识别。
#
# 为什么需要它：authorized 是写库闸门，而它原本**只由模型给出**。
# 实测模型会误判 ——「帮我预约明天上午 10 点到 12 点的计算机实验室」这种
# 措辞明确、要素齐全的请求，模型也返回过 authorized=false。
# 后果不是「少做一步」，而是 _sanitize_plan 把 create_reservation 静默摘掉，
# Agent 全程只查不写、还去 verify 一个不存在的预约，最后如实汇报
# 「数据库中没有找到对应预约记录」—— 用户明确下了单，系统什么都没做。
#
# 关键区分：authorized 判断的是「用户有没有吩咐我们去写库」，这是**语言问题**；
# 「这个用户有没有权限写库」是**权限问题**，属于 check_user_permission 的职责。
# 语言问题用规则兜底比赌模型可靠得多，所以这里做确定性识别，与模型判断取并集。
_BOOKING_VERB_PATTERN = re.compile(r"预约|预订|预定|帮我约|我要约|帮我订|帮我定")

# 疑问语气标记。
#
# 这是排除「在询问而非在下单」的关键：「预约需要什么材料？」「明天能预约吗？」
# 都含预约动词，但它们是**问**，不是**吩咐**。
# 用「有疑问词就否决」比枚举各种祈使句式稳健得多 ——
# 中文的祈使表达千变万化，而疑问标记是有限且稳定的。
_QUESTION_PATTERN = re.compile(
    r"吗|呢|怎么|如何|什么|哪些|多少|几个|为什么|是否|能否|"
    r"能不能|可不可以|可以吗|行不行|有没有|需要准备|条件是|要求是"
)


def explicit_booking_request(text: str) -> bool:
    """用户是不是在字面上要求「替他下单」。

    判据 = 提到预约 + 不是疑问句。
    配合「三要素齐全」（见 _slots_ready_for_booking）一起用：
    三者同时成立才认为可以写库，所以这里可以允许稍宽的动词匹配 ——
    真正兜底的是槽位完整性，而不是措辞的严格程度。
    """
    text = text or ""
    if not _BOOKING_VERB_PATTERN.search(text):
        return False
    return not _QUESTION_PATTERN.search(text)


def _slots_ready_for_booking(slots: dict) -> bool:
    """预约所需的三要素是否齐全（实验室 + 日期 + 起止时间）。

    与授权判断配合使用：只有「用户明确说约」**且**「要素齐全」时才认为可以写库。
    两道条件缺一不可，否则「我打算预约」这种没有时间信息的句子也会被当成指令。
    """
    return all(
        slots.get(key) not in (None, "", [])
        for key in ("lab_name", "date", "start_time", "end_time")
    )


def analyze(ctx: AgentContext, state: AgentState) -> dict:
    """把自然语言解析成 intent + slots + authorized。"""
    query = state.get("user_query") or ""
    today = state.get("today") or datetime.now().strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][
        datetime.strptime(today, "%Y-%m-%d").weekday()
    ]

    if breaker_open(ctx):
        logger.info("模型熔断已开启，需求理解直接走规则兜底")
        return _fallback_analyze(query, today)

    prompt = prompts.ANALYZE_PROMPT.format(
        today=today,
        weekday=weekday,
        history=_history_text(state.get("history")),
        query=query,
    )

    try:
        data = load_json(_ask(ctx, prompt))
    except Exception as exc:  # noqa: BLE001
        # 模型不可用（限流 / 超时 / 返回垃圾）一律走启发式兜底。
        # 这里刻意连 BusinessException 一起吞掉：一个 429 就整轮失败，
        # 在线下演示和线上都是不可接受的 —— 规则兜底至少能把主流程跑完。
        logger.warning("需求解析失败，启用规则兜底：%s", exc)
        return _fallback_analyze(query, today)

    intent = (data.get("intent") or "").strip()
    if intent not in VALID_INTENTS:
        intent = "other"

    slots = data.get("slots") or {}
    if not isinstance(slots, dict):
        slots = {}
    slots = _normalize_slots(slots)

    missing = data.get("missing_slots") or []
    if not isinstance(missing, list):
        missing = []

    authorized = bool(data.get("authorized"))
    # 模型说「不授权」，但用户原话就是明确的下单指令、三要素也齐全 ——
    # 以确定性结论为准。理由见 explicit_booking_request 的注释：
    # 误判成 false 的代价是「用户要求的事系统完全没做」，
    # 而放宽这一次的代价只是「多走一遍 check_user_permission + 时段校验」，
    # 两道校验仍然会挡住真正不该写的请求。
    if (
        not authorized
        and intent == "reserve_lab"
        and _slots_ready_for_booking(slots)
        and explicit_booking_request(query)
    ):
        logger.warning(
            "模型判定 authorized=false，但用户原话是明确预约指令且要素齐全，"
            "改用确定性结论放行（否则 create_reservation 会被静默摘除）"
        )
        authorized = True

    return {
        "intent": intent,
        "slots": slots,
        "missing_slots": [str(item) for item in missing],
        "authorized": authorized,
        "reason": str(data.get("reason") or ""),
    }


def _normalize_slots(slots: dict) -> dict:
    """清理槽位：把 "null"/"无" 这类字符串当成真正的空值。

    模型很爱把 null 写成字符串 "null"，不清掉的话后面判断会全部失效。
    """
    cleaned: dict[str, Any] = {}
    for key, value in slots.items():
        if isinstance(value, str):
            text = value.strip()
            if text.lower() in {"", "null", "none", "无", "未知", "n/a", "未提及"}:
                continue
            cleaned[key] = text
        elif value is not None:
            cleaned[key] = value
    return cleaned


# 时间表达正则：抠实验室名之前要先把它抹掉。
# 「下午2点到5点的计算机实验室」如果不先处理，正则会把“点”一样的
# 汉字一起吃掉，抠出「午2点到5点的计算机」这种鬼东西。
_TIME_NOISE = re.compile(
    r"(上午|下午|中午|晚上|凌晨|明早|明晚)?\s*"
    r"\d{1,2}\s*[:：点时]\s*(半|\d{1,2}\s*分?)?"
    r"(\s*(到|至|~|—|-)\s*\d{1,2}\s*[:：点时]\s*(半|\d{1,2}\s*分?)?)?"
)

# 抠出来的名字可能还带着助词、代词或动词，逐个剥掉。
# 按长度倒序排列：否则“预约”会被“约”先匹配掉一半，剩下一个“预”字。
_LAB_LEAD_NOISE = tuple(
    sorted(
        (
            "麻烦你",
            "麻烦",
            "帮我",
            "我要",
            "我想",
            "预约",
            "预订",
            "预定",
            "申请",
            "查询",
            "查看",
            "了解",
            "使用",
            "租用",
            "借用",
            "一下",
            "请",
            "我",
            "帮",
            "要",
            "想",
            "的",
            "个",
            "这",
            "那",
            "了",
            "家",
            "间",
            "门",
            "约",
        ),
        key=len,
        reverse=True,
    )
)


def _extract_lab_name(text: str) -> str | None:
    """从口语里抠出实验室名，例如「明天下午2点的计算机实验室」→ 计算机实验室。"""
    cleaned = _TIME_NOISE.sub(" ", text or "")
    match = re.search(r"([\u4e00-\u9fa5]{2,8})实验室", cleaned)
    if not match:
        return None

    name = match.group(1)
    changed = True
    while changed:
        changed = False
        for noise in _LAB_LEAD_NOISE:
            if name.startswith(noise):
                name = name[len(noise):]
                changed = True
                break
    return f"{name}实验室" if len(name) >= 2 else None


def _fallback_analyze(query: str, today: str) -> dict:
    """规则兜底：模型不给力时，用关键词 + 正则硬解。

    写得“笨”一点没关系 —— 它的使命是在模型不可用时保证主流程跑得通。
    """
    text = query or ""
    intent = "other"
    if re.search(r"预约|预订|预定|约一下|帮我约|我要约", text):
        intent = "reserve_lab"
    elif re.search(r"我的预约|预约记录|查.*预约", text):
        intent = "query_my_reservation"
    elif re.search(r"取消", text):
        intent = "cancel_reservation"
    elif re.search(r"设备|仪器", text):
        intent = "query_equipment"
    elif re.search(r"规则|安全|规范|制度|注意", text):
        intent = "query_rules"
    elif re.search(r"实验室|开放", text):
        intent = "query_lab"

    slots: dict[str, Any] = {}

    # 日期：今天 / 明天 / 后天 / 具体日期
    base = datetime.strptime(today, "%Y-%m-%d")
    if "后天" in text:
        slots["date"] = (base + timedelta(days=2)).strftime("%Y-%m-%d")
    elif "明天" in text:
        slots["date"] = (base + timedelta(days=1)).strftime("%Y-%m-%d")
    elif "今天" in text:
        slots["date"] = today
    else:
        matched = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if matched:
            slots["date"] = (
                f"{matched.group(1)}-{int(matched.group(2)):02d}-"
                f"{int(matched.group(3)):02d}"
            )

    # 时间：支持 "14:00-17:00"、"下午2点到5点"、"14点至17点"
    times = re.findall(r"(\d{1,2})\s*[:：点]\s*(\d{2})?", text)
    hours = []
    for hour_text, minute_text in times:
        hour = int(hour_text)
        minute = int(minute_text) if minute_text else 0
        # "下午2点" 要加 12
        if hour <= 12 and re.search(r"下午|晚上", text) and hour < 12:
            hour += 12
        hours.append((hour, minute))
    if hours:
        slots["start_time"] = f"{hours[0][0]:02d}:{hours[0][1]:02d}"
    if len(hours) > 1:
        slots["end_time"] = f"{hours[1][0]:02d}:{hours[1][1]:02d}"
    elif len(hours) == 1:
        # 只给了一个时间点 → 默认约 3 小时
        end_hour = hours[0][0] + 3
        if end_hour <= 23:
            slots["end_time"] = f"{end_hour:02d}:{hours[0][1]:02d}"

    # 实验室名：先把时间表达抹掉再找，否则“点”会被一起吞进去。
    lab_name = _extract_lab_name(text)
    if lab_name:
        slots["lab_name"] = lab_name

    multiple = re.search(r"(\d+)\s*个?\s*小时", text)
    if multiple:
        slots["duration_hours"] = int(multiple.group(1))

    missing = []
    if intent == "reserve_lab":
        if not slots.get("lab_name"):
            missing.append("lab_name")
        if not slots.get("date"):
            missing.append("date")

    authorized = bool(
        intent == "reserve_lab"
        and _slots_ready_for_booking(slots)
        and explicit_booking_request(text)
    )

    return {
        "intent": intent,
        "slots": slots,
        "missing_slots": missing,
        "authorized": authorized,
        "reason": "（模型输出无法解析，已使用规则兜底）",
    }


# ---------------------------------------------------------------------------
# 节点 2：任务规划
# ---------------------------------------------------------------------------


def make_plan(ctx: AgentContext, state: AgentState, extra: str = "") -> list[PlanStep]:
    """生成执行计划。产出一定会经过白名单过滤和步数裁剪。"""
    # 只读恢复轮是「系统强制触发」的确定性流程，不是模型的自由创作：
    # 本轮唯一任务就是查替代时段，答案早就写在代码里了。
    # 既然结果已知，就没有理由再花一次模型调用去问 —— 省下的额度
    # 留给真正需要推理的环节（这也是整轮调用数能从 6 降到 4 的原因）。
    if state.get("read_only"):
        logger.info("只读恢复轮：使用确定性计划，跳过模型规划")
        return _fallback_plan(state)

    if breaker_open(ctx):
        logger.info("模型熔断已开启，任务规划直接走兜底计划")
        return _fallback_plan(state)

    prompt = prompts.PLAN_PROMPT.format(
        tool_catalog=tool_catalog(),
        max_steps=MAX_PLAN_STEPS,
        query=state.get("user_query") or "",
        intent=state.get("intent") or "other",
        slots=_slots_text(state.get("slots") or {}),
        missing_slots=state.get("missing_slots") or [],
        memory=state.get("memory_context") or "（暂无）",
        extra=extra,
    )

    try:
        data = load_json(_ask(ctx, prompt))
        raw_plan = data.get("plan") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("任务规划失败，启用兜底计划：%s", exc)
        return _fallback_plan(state)

    plan = _sanitize_plan(raw_plan, state)
    return plan or _fallback_plan(state)


def _sanitize_plan(raw_plan: Any, state: AgentState) -> list[PlanStep]:
    """白名单过滤 + 步数裁剪 + 授权闸门。

    这是「LLM 提出的计划」和「系统真正执行的计划」之间的关卡。
    模型可以随便说，但只有注册过、且被允许的工具才能进执行队列。
    """
    if not isinstance(raw_plan, list):
        return []

    authorized = bool(state.get("authorized"))
    plan: list[PlanStep] = []

    for index, item in enumerate(raw_plan, start=1):
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        if tool not in TOOL_REGISTRY:
            logger.warning("计划中出现未注册的工具，已丢弃：%s", tool)
            continue
        # 授权闸门 + 只读闸门：没拿到用户明确授权、或本轮是只读轮次，
        # 绝不允许出现写库操作。模型可以随便想，但执行队列由系统把关。
        if tool == "create_reservation" and (
            not authorized or state.get("read_only")
        ):
            # 用 warning 而不是 info：这条日志意味着用户明确要求的预约
            # 很可能不会发生，是最需要被看见的一类降级，不能埋在 info 里。
            logger.warning(
                "已从计划中移除 create_reservation（authorized=%s, read_only=%s）"
                "—— 用户将拿不到预约结果，请确认这是预期行为",
                authorized,
                bool(state.get("read_only")),
            )
            continue
        args = item.get("args")
        args = args if isinstance(args, dict) else {}
        # 计划阶段就把「已确定的槽位」预填进去，能显著减少后面的模型调用：
        # node_route 只在参数不全时才补全，参数一齐全程 0 次 LLM 调用。
        args = backfill_args(args, state, tool)
        plan.append(
            PlanStep(
                id=len(plan) + 1,
                tool=tool,
                reason=str(item.get("reason") or ""),
                args=args,
                status="pending",
            )
        )
        if len(plan) >= MAX_PLAN_STEPS:
            break

    return _ensure_write_step(plan, state)


def _ensure_write_step(plan: list[PlanStep], state: AgentState) -> list[PlanStep]:
    """给「明确的预约请求」补齐写库步骤。

    为什么要在计划生成之后再补一刀：实测模型会**整步漏掉** create_reservation，
    生成出这种自相矛盾的计划 ——

        list_open_labs → check_user_permission → query_lab_availability
        → verify_reservation          ← 没有创建，却去验证

    接着 reflect 发现「验证不到记录」，判定 replan，重新规划又把这一步漏掉，
    如此往复直到耗尽重规划轮次。全程用户只拿到查询结果，预约从未发生。

    「该不该有这一步」的依据是完全确定性的（意图 + 槽位 + 用户原话），
    不该赌模型记不记得，所以这里由系统补齐。
    写库的安全性仍然由 authorized 闸门保证 —— 它现在既听模型的，
    也听用户原话的确定性判断。
    """
    if state.get("read_only") or state.get("intent") != "reserve_lab":
        return plan
    if not state.get("authorized"):
        return plan
    if not _slots_ready_for_booking(state.get("slots") or {}):
        return plan
    if any(step.get("tool") == "create_reservation" for step in plan):
        return plan
    # 计划为空说明模型整段没产出，这种情况必须整份换成兜底计划
    # （它自带完整的查→验→写→验流程），而不是只补一个写库步骤 ——
    # 那会得到一个没有前置校验的裸写库计划。
    if not plan:
        return plan

    write = PlanStep(
        id=0,
        tool="create_reservation",
        reason="用户已明确下达预约指令且要素齐全，系统补齐写库步骤（模型遗漏）",
        args=backfill_args({}, state, "create_reservation"),
        status="pending",
    )

    # 插在第一个 verify_reservation 之前 —— 先写后验，顺序不能反；
    # 计划里没有验证步骤时追加到末尾。
    for index, step in enumerate(plan):
        if step.get("tool") == "verify_reservation":
            plan.insert(index, write)
            break
    else:
        plan.append(write)

    logger.warning("模型计划缺少 create_reservation，已由系统补齐（避免只查不约）")

    # 补一步可能超出步数上限：从后往前裁掉非关键步骤，
    # 写库与验证是这一轮的核心，必须保留。
    while len(plan) > MAX_PLAN_STEPS:
        for index in range(len(plan) - 1, -1, -1):
            if plan[index].get("tool") not in ("create_reservation", "verify_reservation"):
                plan.pop(index)
                break
        else:
            break

    # 重新编号，保证轨迹里的步骤 id 连续、前端能按序渲染
    for index, step in enumerate(plan, start=1):
        step["id"] = index
    return plan


def _fallback_plan(state: AgentState) -> list[PlanStep]:
    """兜底计划：按意图给出确定性的标准流程。

    即使模型完全不可用，预约主流程依然能跑通 —— 只是少了「按需裁剪」的智能。
    """
    intent = state.get("intent") or "other"
    slots = state.get("slots") or {}
    authorized = bool(state.get("authorized"))

    def step(index: int, tool: str, reason: str, args: dict | None = None) -> PlanStep:
        return PlanStep(
            id=index,
            tool=tool,
            reason=reason,
            args=args or {},
            status="pending",
        )

    # 只读轮次（目标时段被占用后的强制重规划）：
    # 本轮唯一任务是把替代时段查回来、由回复层推荐给用户。
    # 这里刻意不包含 create_reservation —— 替用户“自作主张换时间下单”
    # 比“不给结果”更糟。
    if state.get("read_only"):
        return [
            step(
                1,
                "find_available_slots",
                "目标时段已被占用，查找并推荐其它空闲时段",
                {
                    "lab_name": slots.get("lab_name"),
                    "date": slots.get("date"),
                    "duration_hours": _duration_hours(slots),
                    "preferred_start": slots.get("start_time"),
                },
            )
        ]

    if intent == "reserve_lab":
        plan = [
            step(
                1,
                "query_lab_availability",
                "先确认目标实验室在该时段是否可约",
                {
                    "lab_name": slots.get("lab_name"),
                    "date": slots.get("date"),
                    "start_time": slots.get("start_time"),
                    "end_time": slots.get("end_time"),
                },
            ),
            step(
                2,
                "check_user_permission",
                "确认用户具备预约资格且未超配额",
                {"lab_name": slots.get("lab_name")},
            ),
        ]
        if authorized:
            plan.append(
                step(
                    3,
                    "create_reservation",
                    "时段可用且权限通过，创建预约",
                    {
                        "lab_name": slots.get("lab_name"),
                        "date": slots.get("date"),
                        "start_time": slots.get("start_time"),
                        "end_time": slots.get("end_time"),
                        "equipment_name": slots.get("equipment_name"),
                    },
                )
            )
            plan.append(
                step(
                    4,
                    "verify_reservation",
                    "回库核验预约是否真的创建成功",
                    {
                        "lab_name": slots.get("lab_name"),
                        "date": slots.get("date"),
                    },
                )
            )
        return plan

    if intent == "query_equipment":
        return [
            step(
                1,
                "list_lab_equipments",
                "查询该实验室的设备清单",
                {
                    "lab_name": slots.get("lab_name"),
                    "keywords": slots.get("keywords"),
                },
            )
        ]

    if intent == "query_rules":
        return [
            step(
                1,
                "search_lab_docs",
                "从知识库检索相关制度资料",
                {"query": state.get("user_query") or ""},
            )
        ]

    if intent == "query_lab":
        return [
            step(
                1,
                "list_open_labs",
                "列出开放中的实验室",
                {"keywords": slots.get("keywords") or slots.get("lab_name") or ""},
            )
        ]

    if intent == "query_my_reservation":
        return [
            step(
                1,
                "list_open_labs",
                "确认可预约范围",
                {},
            )
        ]

    if intent == "cancel_reservation":
        return []

    return [
        step(
            1,
            "list_open_labs",
            "先了解系统里有哪些开放实验室",
            {},
        )
    ]


# ---------------------------------------------------------------------------
# 节点 3：工具路由（补全参数）
# ---------------------------------------------------------------------------


# 需要从「需求理解」阶段回填的参数。
# 这些值都是用户亲口给出的，属于已知事实，不能在路由阶段丢失。
_SLOT_ARG_KEYS = (
    "lab_name",
    "equipment_name",
    "date",
    "start_time",
    "end_time",
    "duration_hours",
    "keywords",
)

# 槽位名 → 工具参数名 的别名映射。
# 需求理解阶段抽出的是用户的原始表述（start_time），工具需要的可能是另一种
# 叫法（preferred_start），这里把两边对上，不靠模型自觉。
_SLOT_ALIASES = {"preferred_start": "start_time"}


def resolve_args(
    ctx: AgentContext, state: AgentState, step: PlanStep
) -> tuple[dict, str]:
    """补全这一步的参数，返回 (args, reason)。

    计划里参数已经齐了的步骤不会走到这里（graph 会直接执行），
    但 graph 在那条捷径上也会调用 backfill_args 兼一道底。
    """
    tool_name = _find_tool_name(step)
    tool = TOOL_REGISTRY.get(tool_name)
    observations = "\n".join(state.get("observations") or []) or "（暂无执行结果）"

    if breaker_open(ctx):
        logger.info("模型熔断已开启，第 %s 步沿用计划里的参数", step.get("id"))
        return (
            backfill_args(dict(step.get("args") or {}), state, tool_name),
            "（模型连续失败，沿用计划参数）",
        )

    prompt = prompts.ROUTE_PROMPT.format(
        step_index=(step.get("id") or 1),
        tool_name=tool_name,
        tool_desc=tool.description if tool else "",
        planned_args=json.dumps(step.get("args") or {}, ensure_ascii=False),
        parameters=json.dumps(
            tool.parameters if tool else {}, ensure_ascii=False, indent=2
        ),
        observations=observations,
        query=state.get("user_query") or "",
    )

    try:
        data = load_json(_ask(ctx, prompt))
        args = data.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        reason = str(data.get("reason") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("参数补全失败，沿用计划里的原始参数：%s", exc)
        args = {}
        reason = "（参数补全失败，沿用计划参数）"

    # 优先级：计划里写死的参数 > 模型现场补全 > 需求理解阶段抽出的槽位。
    # 两层都把空值（None/""/[]）滤掉 —— 模型很喜欢在 JSON 里填 null，
    # 一旦让 null 覆盖掉已知值，工具就会拿 None 去查库，白忙一场。
    merged: dict = {}
    for source in (args, step.get("args") or {}):
        for key, value in source.items():
            if value not in (None, "", []):
                merged[key] = value

    return backfill_args(merged, state, tool_name), reason


def backfill_args(args: dict, state: AgentState, tool_name: str) -> dict:
    """用「需求理解」阶段抽出的槽位，补齐工具参数里的空缺。

    为什么不能只靠模型？因为 lab_name / date 这类值是用户亲口说的，
    属于已知事实。提示词一长，模型很容易把它们填成 null。
    这里做一道确定性兜底：只要工具声明了这个参数、而当前值是空，
    就用槽位顶上。
    """
    tool = TOOL_REGISTRY.get(tool_name)
    allowed = set((tool.parameters.get("properties") or {}).keys()) if tool else set()
    slots = state.get("slots") or {}

    filled = dict(args or {})
    for key in (*_SLOT_ARG_KEYS, *_SLOT_ALIASES):
        if key not in allowed or filled.get(key) not in (None, "", []):
            continue
        value = slots.get(_SLOT_ALIASES.get(key, key))
        if value not in (None, "", []):
            filled[key] = value

    # 工具没声明过的字段一律丢掉，别把垃圾参数带进业务层
    if allowed:
        filled = {k: v for k, v in filled.items() if k in allowed}
    return filled


# ---------------------------------------------------------------------------
# 节点 5：反思
# ---------------------------------------------------------------------------


def should_replan(state: AgentState) -> str | None:
    """确定性重规划判定：返回重规划理由，或 None 表示不需要。

    为什么不让模型自己决定“要不要再试一次”？因为这是业务规则，不是语言
    问题。同一个请求今天重规划、明天直接放弃，演示时会上不了台。

    这里只硬编码最关键的场景：用户想预约，实验室存在且营业，但目标时段
    被占用（或建单失败）—— 那就必须去查替代时段并推荐，而不是直接说
    “约不上”。
    """
    # 已经跑过一轮只读探索了，别再循环
    if state.get("read_only"):
        return None
    if (state.get("replan_round") or 0) >= MAX_REPLAN_ROUNDS:
        return None
    if state.get("intent") != "reserve_lab" or not state.get("authorized"):
        return None

    results = state.get("tool_results") or {}
    if (results.get("create_reservation") or {}).get("ok"):
        return None

    availability = results.get("query_lab_availability") or {}
    if availability.get("ok") is not True or availability.get("lab_open") is False:
        return None

    requested = availability.get("requested") or {}
    if requested.get("available"):
        # 时段是空的，建单失败一定是别的原因（权限、配额…），交给模型判断
        return None

    window = f"{requested.get('start_time', '')}-{requested.get('end_time', '')}"
    return (
        f"目标时段 {window} {requested.get('conflict_reason') or '不可用'}，"
        "需要查找该实验室的替代空闲时段并推荐给用户"
    )


def _duration_hours(slots: dict) -> float | None:
    """算出用户想要的时长（小时），拿不到就返回 None，让工具用默认值。"""
    hours = slots.get("duration_hours")
    try:
        if hours:
            return float(hours)
    except (TypeError, ValueError):
        pass

    start, end = slots.get("start_time"), slots.get("end_time")
    if not start or not end:
        return None
    try:
        delta = datetime.strptime(end, "%H:%M") - datetime.strptime(start, "%H:%M")
    except ValueError:
        return None
    return max(delta.total_seconds() / 3600, 0.5)


def render_execution_log(state: AgentState) -> str:
    """把计划 + 结果渲染成反思/回复用的文本。"""
    plan = state.get("plan") or []
    results = state.get("tool_results") or {}
    lines = []
    for step in plan:
        tool = _find_tool_name(step)
        status = step.get("status") or "pending"
        mark = {"done": "✓", "failed": "✗", "skipped": "-"}.get(status, "·")
        lines.append(f"{mark} 第{step.get('id')}步 {tool_label(tool)}（{tool}）")
        if step.get("reason"):
            lines.append(f"   目的：{step['reason']}")
        outcome = results.get(tool)
        if outcome is not None:
            lines.append(
                "   结果：" + json.dumps(outcome, ensure_ascii=False)[:800]
            )
    return "\n".join(lines) if lines else "（没有执行任何步骤）"


def reflect(ctx: AgentContext, state: AgentState) -> dict:
    """检查执行结果，给出 finish / replan 判定。"""
    # 只读恢复轮跑完一定是收尾：能查的都查到了，剩下的只是「把结果讲清楚」。
    # 此时再问模型一次“该不该重规划”，期望的答案永远是 finish，纯属浪费。
    if state.get("read_only"):
        logger.info("只读恢复轮：使用确定性反思，跳过模型调用")
        return {
            "verdict": "finish",
            "reflection": "替代时段已查回，不再向下走写库操作，转为向用户给出可选项。",
        }

    if breaker_open(ctx):
        logger.info("模型熔断已开启，反思环节直接收尾")
        return {
            "verdict": "finish",
            "reflection": "（模型连续失败，不再重试，直接输出已有结果）",
        }

    prompt = prompts.REFLECT_PROMPT.format(
        query=state.get("user_query") or "",
        intent=state.get("intent") or "other",
        execution_log=render_execution_log(state),
        replan_round=state.get("replan_round") or 0,
    )

    try:
        data = load_json(_ask(ctx, prompt))
    except Exception as exc:  # noqa: BLE001
        logger.warning("反思失败，按完成处理：%s", exc)
        return {
            "verdict": "finish",
            "reflection": "（反思环节调用失败，直接输出已有结果）",
        }

    verdict = (data.get("verdict") or "finish").strip().lower()
    if verdict not in {"finish", "replan"}:
        verdict = "finish"

    reflection = str(data.get("reflection") or "")
    if verdict == "replan" and data.get("replan_reason"):
        reflection = f"{reflection}｜需要调整：{data['replan_reason']}"

    return {"verdict": verdict, "reflection": reflection}


# ---------------------------------------------------------------------------
# 节点 6：生成回复
# ---------------------------------------------------------------------------


def build_respond_prompt(state: AgentState) -> str:
    return prompts.RESPOND_PROMPT.format(
        query=state.get("user_query") or "",
        execution_log=render_execution_log(state),
        reflection=state.get("reflection") or "（无）",
        memory=state.get("memory_context") or "（暂无）",
        history=_history_text(state.get("history")),
    )


async def _chain_first(
    stream: AsyncIterator, first: Any
) -> AsyncIterator:
    """把已经取出来的首包重新拼回流的头部。"""
    yield first
    async for chunk in stream:
        yield chunk


async def stream_response(
    ctx: AgentContext, state: AgentState, emit: Callable[[str], None]
) -> str:
    """流式生成最终回复。emit 每产出一段文字就被调用一次。

    这里有两条不能让步的边界：

    1. **一旦已经往前端吐过字，就绝不再重试** —— 否则用户会看到同一段话
       被写了两次。
    2. **首包必须有硬超时** —— 实测供应商会“连得上但不吐字”，
       无限等下去只会把整轮对话拖成几十秒的空白。
    """
    if breaker_open(ctx):
        # 前面几个节点已经连续失败，再等一个首包超时只是白花时间，
        # 直接让上层用本地摘要收尾。
        raise BusinessException(message="模型连续失败，跳过流式生成")

    prompt = build_respond_prompt(state)
    llm = get_llm(streaming=True)
    parts: list[str] = []

    # 只试一次，**不重试**。
    #
    # 这个决定和上面的结构化环节刚好相反，理由是位置不同：
    #   - respond 是流程的最后一步，它失败有确定性兜底接着（stream_response_safe），
    #     代价是「文案生硬一点」，不是「任务做不成」；
    #   - 而重试的代价是用户对着空白屏幕干等。实测这个供应商连接被重置时
    #     每次尝试约 5s，加上退避 2s + 6s，三次共 23s，最后往往还是失败。
    # 与其用 23s 换一条兜底文案，不如 5s 就给出兜底文案。
    started = time.monotonic()
    stream = llm.astream(prompt).__aiter__()

    try:
        # 首包单独计时：它才是判断“这一路通不通”的唯一信号。
        first = await asyncio.wait_for(
            stream.__anext__(), timeout=settings.LLM_FIRST_TOKEN_TIMEOUT
        )
    except StopAsyncIteration:
        # 模型返回空内容：不算失败，交给上层判断是否降级
        return "".join(parts).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("流式回复首包失败（%.1fs）：%s", time.monotonic() - started, exc)
        raise BusinessException(message=f"生成回复失败：{exc}") from exc

    try:
        async for chunk in _chain_first(stream, first):
            text = chunk_text(chunk)
            if text:
                parts.append(text)
                emit(text)
        return "".join(parts).strip()
    except Exception as exc:  # noqa: BLE001
        if parts:
            # 已经吐出去的字是真的，不能说不要就不要 ——
            # 半截回复也比一条兜底模板更接近用户的诉求。
            logger.warning("流式回复中断，保留已产出的 %s 段：%s", len(parts), exc)
            return "".join(parts).strip()
        logger.warning("流式回复中断且无产出：%s", exc)
        raise BusinessException(message=f"生成回复失败：{exc}") from exc


async def stream_response_safe(
    ctx: AgentContext, state: AgentState, emit: Callable[[str], None]
) -> str:
    """带兜底的回复生成：模型挂了也要给用户一个能看的结论。"""
    try:
        text = await stream_response(ctx, state, emit)
        if text:
            _record(ctx, ok=True)
            return text
    except Exception:  # noqa: BLE001
        _record(ctx, ok=False)
        logger.warning("流式生成回复失败，降级为本地摘要")

    # 降级：直接把执行结果拼成回复，虽然生硬但信息是真实的
    fallback = _compose_fallback_reply(state)
    if fallback:
        emit(fallback)
    return fallback


def _compose_fallback_reply(state: AgentState) -> str:
    """模型不可用时，用**真实执行结果**拼一条能看的回复。

    这段文案是演示的底线。模型可以挂，但用户必须清楚地知道三件事：
    诉求能不能满足、替代方案是什么、下一步该做什么。
    它回答的是「业务问题」，而不是罗列「我调了哪些工具」——
    后者是日志，不是回复。
    """
    results = state.get("tool_results") or {}
    slots = state.get("slots") or {}

    availability = results.get("query_lab_availability") or {}
    requested = availability.get("requested") or {}
    permission = results.get("check_user_permission") or {}
    created = results.get("create_reservation") or {}
    alternatives = results.get("find_available_slots") or {}
    verification = results.get("verify_reservation") or {}

    lab_name = slots.get("lab_name") or availability.get("lab_name") or "该实验室"
    date = slots.get("date") or availability.get("date") or ""
    window = f"{slots.get('start_time') or requested.get('start_time') or ''}-" \
             f"{slots.get('end_time') or requested.get('end_time') or ''}"
    window = window.strip("-")
    when = f"{date} {window}".strip()

    lines: list[str] = []

    # 1. 先回答「能不能满足诉求」
    if permission.get("ok") is False:
        lines.append(f"暂时没法帮你预约：{permission.get('error')}。")
    if availability.get("ok") is False:
        lines.append(f"查询实验室状态失败：{availability.get('error')}。")
    elif availability.get("lab_open") is False:
        lines.append(f"{lab_name}当前未开放，暂时不能预约。")
    elif requested and requested.get("available") is False:
        lines.append(
            f"{when} 的{lab_name}{requested.get('conflict_reason') or '不可用'}，"
            "所以这一单我没有提交。"
        )

    # 2. 再给替代方案
    if alternatives.get("slots"):
        pretty = "、".join(
            f"{item['start_time']}-{item['end_time']}"
            for item in alternatives["slots"]
        )
        lines.append(
            f"我查了{alternatives.get('lab_name') or lab_name}当天的其它空闲时段：{pretty}。"
        )
        lines.append("如果你可以改时间，告诉我选哪一段，我马上帮你提交预约。")
    elif alternatives.get("ok") and alternatives.get("has_slot") is False:
        lines.append(f"{alternatives.get('date') or date} 当天{lab_name}已经排满了，换一天可能更合适。")

    # 3. 成功的预约要把结果说清楚
    if created.get("ok"):
        lines.append(
            f"预约已提交：{created.get('lab_name')} {created.get('date')} "
            f"{created.get('start_time')}-{created.get('end_time')}，"
            f"当前状态「{created.get('status')}」，等管理员审核通过后即可使用。"
        )
        if verification.get("ok") and verification.get("exists"):
            lines.append(
                f"我已经回查数据库确认过：预约编号 {verification.get('reservation_id')}，"
                f"状态「{verification.get('status_text')}」，记录真实存在。"
            )

    # 4. 最后把工具层面的意外失败如实交代，不藏
    #    （已经被上面解释过的失败不再重复播报，否则就成了噪音）
    conflict_explained = bool(requested and requested.get("available") is False)
    quiet = {"query_lab_availability", "check_user_permission", "find_available_slots"}
    for tool, outcome in results.items():
        if outcome.get("ok") is not False or tool in quiet:
            continue
        if tool == "create_reservation" and conflict_explained:
            continue
        lines.append(f"（{tool_label(tool)}这一步失败了：{outcome.get('error')}）")

    missing = state.get("missing_slots") or []
    if missing:
        lines.append(f"还需要你补充：{'、'.join(str(item) for item in missing)}。")

    # 5. 走到这里说明这一轮压根不是预约类需求（比如「你能做什么」），
    #    上面那套预约叙事一句都用不上。此时绝不能回一句
    #    「没拿到结果，换个说法再试试」—— 用户没做错任何事，
    #    而且工具其实已经查回了真实数据，白白丢掉才是浪费。
    if not lines:
        lines.extend(_summarize_results(results))
    if not lines:
        lines.append(_CAPABILITY_HINT)
    return "\n".join(lines)


def _summarize_results(results: dict) -> list[str]:
    """把非预约类工具的真实返回值翻译成人话，给兜底回复用。

    只覆盖「查回来就能直接说清楚」的那几个查询类工具；
    预约类的叙事已经在 _compose_fallback_reply 里单独处理了。
    """
    lines: list[str] = []

    labs = results.get("list_open_labs") or {}
    if labs.get("ok") and labs.get("labs"):
        names = "、".join(str(item.get("name", "")) for item in labs["labs"])
        total = labs.get("total") or len(labs["labs"])
        lines.append(f"当前系统里有 {total} 个实验室：{names}。")

    equipments = results.get("list_lab_equipments") or {}
    if equipments.get("ok") and equipments.get("equipments"):
        names = "、".join(str(item.get("name", "")) for item in equipments["equipments"])
        lines.append(f"{equipments.get('lab_name')}的设备有：{names}。")

    docs = results.get("search_lab_docs") or {}
    if docs.get("ok") and docs.get("documents"):
        snippet = " ".join(str(docs["documents"][0].get("content", "")).split())
        if snippet:
            lines.append(f"实验室规章里有一条相关说明：{snippet[:120]}……")

    return lines


# 连工具都没查回东西时，至少要讲清楚「我能做什么」，
# 并给一句可以直接抄的示例 —— 这比让用户去猜有用得多。
_CAPABILITY_HINT = (
    "我目前能帮你做这些事：查询实验室开放状态、检查你的预约权限、"
    "按时间挑出空闲时段、提交预约，并回查数据库确认预约真的写进去了。\n"
    "你可以直接这样问我：帮我预约明天下午 2 点到 5 点的计算机实验室，"
    "如果没有空闲设备就推荐其他时间。"
)
