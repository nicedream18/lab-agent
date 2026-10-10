"""Agent 的 LLM 交互层。

职责边界很明确：**这里只负责「和模型对话」，不碰数据库、不决定控制流**
（控制流在 graph.py，业务逻辑在 tools.py / services）。

改造说明（v2，native tool calling）：
v1 里这个文件承载了一整套**手写的 JSON 规划协议** —— plan 让模型吐出
{"steps":[{"tool":...,"args":{...}}]}、route 再吐一次工具名与参数、reflect 吐
{"verdict":"..."}、respond 吐最终文案，每个环节都配一份提示词和一份「模型不
听话时顶上」的规则降级路径。

v2 把这套协议整层删掉，改由模型直接返回 tool_calls：模型输出的工具名和参数
**就是调用指令本身**，中间不再夹一层「模型说它想干什么 → 代码补全参数 → 代码
执行」。被删掉的不只是文案，而是「意图」与「实际调用」之间的那道缝。

留在这里的只有两件事：
    analyze(ctx, state)            —— 用户原话 → slots / missing_slots / authorized
    agent_step(ctx, state, ...)    —— 一次 bind_tools 之后的模型调用，返回带
                                      tool_calls 的 AIMessage

其余环节（执行工具、决定要不要再来一轮、产出最终回复）全部在 graph.py。

为什么 analyze 还保留结构化输出？因为它产出的东西**不是给模型看的，是给系统
用的**：authorized 是写库闸门的一道判断，missing_slots 要用来生成追问话术。
这类结论必须能被代码读取，也就必须落在一个固定格式里。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.agent import prompts, tools
from app.agent.state import (
    MAX_PLAN_STEPS,
    WRITE_TOOLS,
    AgentContext,
    AgentState,
)
from app.common.exceptions import BusinessException
from app.config import settings
from app.utils.net import prefer_ipv4

logger = logging.getLogger(__name__)

# 让大模型域名走 IPv4（见 app/utils/net.py 里的原因说明）。
# 放在模块级而不是某个函数里：必须早于任何一次异步出网请求生效。
if settings.LLM_FORCE_IPV4:
    prefer_ipv4(settings.LLM_BASE_URL)

# 槽位字段名 → 中文。
# 槽位名（lab_name / date / start_time …）是**给机器看的契约**，出现在
# JSON、日志和 SSE 事件里都没问题；但它绝不该出现在给用户看的句子里 ——
# 用户不知道 "date" 是什么。所有面向用户的出口（追问话术、提示词、
# 执行轨迹）都必须先过 slot_label() 翻译，就像 tools.tool_label() 那样。
# 前端 src/utils/agentTrace.js 里有一份同构的 SLOT_LABELS，改这里时记得同步。
SLOT_LABELS = {
    "lab_name": "实验室",
    "equipment_name": "设备",
    "date": "预约日期",
    "start_time": "开始时间",
    "end_time": "结束时间",
    "duration_hours": "使用时长",
    "keywords": "关键词",
}

# 追问缺失信息时给的例子。只说「请补充预约日期」用户还是不知道
# 该用什么格式回答，直接把说法摆出来最省事。
SLOT_HINTS = {
    "lab_name": "比如「软件工程实验室」",
    "equipment_name": "比如「示波器」",
    "date": "比如「明天」或「10 月 9 日」",
    "start_time": "比如「下午 2 点」",
    "end_time": "比如「到 5 点」",
    "duration_hours": "比如「约 3 小时」",
    "keywords": "",
}


def slot_label(name: Any) -> str:
    """把槽位字段名翻译成中文；认不出来的原样返回，不把信息丢掉。"""
    key = str(name).strip()
    return SLOT_LABELS.get(key, key)


def describe_missing(missing: list | None) -> str:
    """把缺失槽位列表拼成一句中文，带例子。

    例：['date'] → 「预约日期（比如「明天」或「10 月 9 日」）」
    """
    parts: list[str] = []
    for item in missing or []:
        key = str(item).strip()
        if not key:
            continue
        label = slot_label(key)
        hint = SLOT_HINTS.get(key)
        parts.append(f"{label}（{hint}）" if hint else label)
    return "、".join(parts)


def missing_labels(missing: list | None) -> list[str]:
    """缺失槽位的中文标签列表，喂给提示词用（模型看到中文就不会原样复述英文键）。"""
    return [slot_label(item) for item in (missing or []) if str(item).strip()]


# ---------------------------------------------------------------------------
# 基础工具函数
# ---------------------------------------------------------------------------


def get_llm(streaming: bool = False) -> ChatOpenAI:
    return ChatOpenAI(
        # 存根要求 SecretStr（运行时 str 其实也能被强转）。
        # 显式包一层既消掉类型报错，也避免密钥在 repr / 日志里裸奔。
        api_key=SecretStr(settings.LLM_API_KEY),
        base_url=settings.LLM_BASE_URL,
        model=settings.LLM_MODEL,
        # 结构化任务 temperature 必须为 0：同样的输入要拿到同样的 JSON，
        # 否则规划结果会飘，轨迹也没法复现。
        temperature=0,
        streaming=streaming,
        # 必须显式设超时。SDK 默认 600 秒，供应商卡住时「不报错」比「报错」更糟 ——
        # 用户会以为前端挂了。15 秒等不来的回答，如实说明比继续等更有价值。
        timeout=settings.LLM_TIMEOUT,
        # 关掉 SDK 自带重试：它会和 planner 的重试循环相乘（2×3=6 次请求），
        # 把最坏等待时间放大到分钟级。重试策略只由 planner 一处掌管，
        # 这样「重试还是放弃」这个决策才可审计、可调。
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
    except Exception as exc:
        logger.exception("调用大模型失败")
        raise BusinessException(message=f"大模型调用失败：{exc}") from exc
    return message_text(response)


# 重试节奏（秒）。首次不等待，之后退避两次。
# 不宜太长：限流窗口往往是分钟级，等 60s 对交互式对话体验是灾难，
# 而真正的止损是下面的熔断，重试只解决“瞬时抖动”。
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
# 一轮对话（analyze + 若干轮 agent）就是几十秒的空白 ——
# 用户看到的是页面卡死，而不是「模型暂时用不了，别等了」。
# 连续失败到阈值就停止向模型要东西，把最坏耗时压到「一次超时」，
# 并如实告诉用户本轮没跑成 —— 这一版没有规则兜底路径。
#
# 阈值取 1 是划算的：计数的粒度是「一次 _ask 彻底放弃」，
# 而 _ask 内部已经把瞬时抖动重试掉了。真正走到这里，说明模型确实不可用。
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


def is_rate_limited(exc: BaseException) -> bool:
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


def is_transient(exc: BaseException) -> bool:
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


# 模型不可用时给用户的唯一交代。
#
# 改造前这里还有另一条路：_fallback_analyze 用正则把用户这句话里的事实抠出来，
# 再让 _fallback_plan 按槽位拼一份确定性计划接着跑完。那条路已经整体删掉 ——
# 与其拿正则猜用户想干什么、再让用户去分辨哪句是模型说的哪句是规则拼的，
# 不如老实说一句「模型暂时不可用」。
MODEL_UNAVAILABLE = "模型暂时不可用，请稍后再试（过一会儿再问我一次就行）。"

# 模型既没调工具、也没给出文字时的兜底话术。正常走不到 —— 理论上模型总会说点什么，
# 真出现了说明它的返回被网关裁成了空。这时候给用户一句能立刻重试的话，
# 比让他对着空白的聊天框发呆好。
EMPTY_REPLY = (
    "抱歉，我没能整理出结论。可以换一种说法再说一次，"
    "或者说清楚实验室、日期和时间段。"
)


_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def today_info(state: AgentState) -> tuple[str, str]:
    """今天的日期与中文星期，两个提示词都要用。

    模型必须知道「今天」是几号，才能把「明天」「下周三」算成具体日期；
    让它自己算一遍再和 system 提示词里写死的那句对上，概率很低。
    """
    today = state.get("today") or datetime.now().strftime("%Y-%m-%d")
    try:
        index = datetime.strptime(today, "%Y-%m-%d").weekday()
    except ValueError:
        index = datetime.now().weekday()
    return today, _WEEKDAYS[index]


def _unavailable() -> dict:
    """模型不可用时的状态产出：置 error，由 graph 直接转去回复节点。

    graph 的 _after_analyze 看到 error 就跳过自主决策 —— 后面的每一个环节
    都要问模型，它不可用，再走一遍只会多花一次超时。

    同时明确写下 plan_needed=False：模型都不可用了，绝无可能去规划，
    _after_analyze 必须把这一轮导向 respond 而不是 plan。
    """
    return {
        "slots": {},
        "missing_slots": [],
        "authorized": False,
        "plan_needed": False,
        "error": MODEL_UNAVAILABLE,
    }


def write_outcomes(results: list[dict] | None) -> list[str]:
    """把「真的写进库里的操作」逐条列成人话。

    这是唯一一处不能省的兜底，而且它不是给用户体验用的：模型在半路挂掉时，
    用户手上可能已经多了一条预约、或者少了一条预约。不把已经发生的事实讲出来，
    他很可能再约一次 —— 那才是真正的数据事故。

    所以这里只陈述已发生的事，不猜、不推荐、不补充。
    """
    lines: list[str] = []
    for item in results or []:
        result = item.get("result") or {}
        if not item.get("ok") or not result.get("ok"):
            continue
        tool = item.get("tool")
        window = (
            f"{result.get('date')} {result.get('start_time')}-"
            f"{result.get('end_time')}"
        )
        if tool == "create_reservation":
            lines.append(
                f"预约已提交（编号 {result.get('reservation_id')}）："
                f"{result.get('lab_name')} {window}，"
                f"当前状态「{result.get('status')}」"
            )
        elif tool == "cancel_reservation":
            lines.append(
                f"预约已取消（编号 {result.get('reservation_id')}）："
                f"{result.get('lab_name')} {window}"
            )
    return lines


# ---------------------------------------------------------------------------
# 节点 1：需求理解
# ---------------------------------------------------------------------------

# 用户「字面上下达写库指令」的确定性识别。
#
# 为什么需要它：authorized 是写库闸门，而它原本**只由模型给出**。
# 实测模型会误判 ——「帮我预约明天上午 10 点到 12 点的计算机实验室」这种
# 措辞明确、要素齐全的请求，模型也返回过 authorized=false。
# 后果不是「少做一步」，而是写库工具被挡在 bind_tools 之外：Agent 全程只查
# 不写、还去 verify 一个不存在的预约，最后如实汇报「数据库中没有找到对应
# 预约记录」—— 用户明确下了单，系统什么都没做。
#
# 关键区分一：authorized 判断的是「用户有没有吩咐我们去写库」，这是**语言问题**；
# 「这个用户有没有权限写库」是**权限问题**，属于 check_user_permission 的职责。
# 语言问题用确定性识别比赌模型可靠得多，所以这里与模型判断取并集。
#
# 关键区分二：这里的规则**不参与选择工具**。选哪个工具、要不要先查再写，
# 完全由模型根据用户意图和工具描述决定（见 AGENT_SYSTEM_PROMPT）。
# 这组正则只回答一个是非题：用户到底有没有吩咐我们改数据。
_BOOKING_VERB_PATTERN = re.compile(
    r"(?<![的。])(预约|预订|预定|帮我约|我要约|帮我订|帮我定|改约|预约个)"
)

# 取消类动词。
#
# 为什么要单独识别：取消是**不可逆**的写库操作，它出问题的形态和预约不一样 ——
# 预约被漏掉，用户只是「没约上」；取消被漏掉，模型却会拿着
# query_user_reservations 查到的「待审核」记录，对用户宣称
# 「已为您取消」——用户以为没事了，预约却还在。
# 所以它必须和 create_reservation 享受同一套确定性识别。
_CANCEL_VERB_PATTERN = re.compile(
    r"(?<![的。])(取消|撤消|撤销|退掉|退订|退了|作废|不借了|不用了)"
)

# 疑问语气标记。
#
# 这是排除「在询问而非在下单」的关键：「预约需要什么材料？」「明天能预约吗？」
# 「怎么取消预约？」都含动词，但它们是**问**，不是**吩咐**。
# 用「有疑问词就否决」比枚举各种祈使句式稳健得多 ——
# 中文的祈使表达千变万化，而疑问标记是有限且稳定的。
_QUESTION_PATTERN = re.compile(
    r"吗|呢|怎么|如何|什么|哪些|多少|几个|为什么|是否|能否|"
    r"能不能|可不可以|可以吗|行不行|有没有|需要准备|条件是|要求是"
)

# ---------------------------------------------------------------------------
# 「读」的框架识别 —— 用来把误判成写库的风险挡住
# ---------------------------------------------------------------------------
#
# 为什么必须单独做这一层：「帮我查一下我的预约记录」里出现了「预约」二字，
# 按动词匹配会被判成「用户在下单」。这个误判方向**比漏判危险得多**：
#
#   漏判（该写没写）  用户拿不到结果，但数据是干净的，用户看得见没办成；
#   误判（不该写却写） 用户只是想看一眼记录，系统却可能真的把预约删了。
#
# 尤其 cancel_reservation 是不可逆的，一条误判就是一条被删掉的预约。
# 所以在「动词匹配 → 写库意图」之间必须再插一道：先把只读片段摘掉，
# 再看剩下的文本里还有没有写库动词。

# 摘除片段用的**填充符**，不能用空串。
#
# 用空串会把被摘掉的两段直接拼在一起，凭空造出一个原文里不存在的祈使结构：
# 「给我讲讲怎么取消预约」摘掉「讲讲怎么取消」后拼接成「给我预约」，
# 而「给我预约」看起来就是一条下单指令 —— 用户只是让讲讲取消流程，
# 系统却去下单了。用句号占位，祈使窗口的字符类排除了「。」，
# 匹配自然被截断，不会跨越摘除区。
_STRIP_FILLER = "。"

# 「查/看 … 预约(记录)」：用户在**读**自己的数据
_READ_FRAME_PATTERN = re.compile(
    r"(帮我|给我|替我|帮忙|麻烦)?"
    r"(查询|查看|查|看看|看|列出|列举|显示|告诉|回顾|统计)"
    r"[^，。？！,?!]{0,10}?"
    r"(预约|预订|预定|记录|历史|情况|状态)"
)

# 「能不能取消 / 可以取消吗」：用户在问「可不可以这么做」，不是让我们动手。
# 注意「吗」类疑问句已经被 _QUESTION_PATTERN 挡掉了，这里补的是
# 不带语气词的陈述式表达（「我想看看能不能取消那个预约」）。
_MODAL_FRAME_PATTERN = re.compile(
    r"(能不能|能否|可不可以|是否可以|可以|能|是否|想知道|想看看)"
    r"[^，。？！,?!]{0,4}"
    r"(取消|撤消|撤销|退掉|退订|作废)"
)

# 「讲讲 / 说说 / 介绍一下…」：用户在**要个说法**，不是要我们动手
# （「给我讲讲怎么取消预约」）。这组词后面常跟动词原形，
# 不摘掉就会被后面的祈使判定当成长。
_EXPLAIN_FRAME_PATTERN = re.compile(
    r"(讲讲|说说|解释|说明|介绍|讲解|科普|教我)"
    r"[^，。？！,?!]{0,6}?"
    r"(取消|撤消|撤销|退掉|退订|作废|预约|预订|预定)"
)

# 「预约 + 信息类名词」：用户在描述他要**看的东西**，不是在提要求。
_RESERVATION_INFO_PATTERN = re.compile(
    r"预约\s*(记录|列表|情况|状态|详情|历史|信息|编号|号)"
)

# ---------------------------------------------------------------------------
# 「祈使」框架识别 —— 用来把漏判（该写没写）的风险挡住
# ---------------------------------------------------------------------------
#
# 上面的「读」框架挡的是误判，这一组挡的是反方向：句子前半段在问、后半段在下单。
#
# 典型例子：「帮我查一下计算机实验室明天下午有没有空，有空就帮我预约」。
# 这句里有「有没有」，会被疑问标记否决；但用户后半句是明确的吩咐。
# 如果因为前半句是问句就放过整个句子，用户就会得到「有空」这个回答，
# 然后**预约根本没有创建** —— 这正是这套系统最容易骗过用户的地方：
# 它给了个像模像样的答复，实际什么都没做。
#
# 所以判据是：只要句子里存在「标志词 + 4 字内跟上动词」的祈使结构，
# 就认定用户在下单，疑问标记不再否决。窗口取 4 是刻意的 ——
# 「帮我查一下…」这种查询里，标志词后面跟的从来不是写库动词。
_IMPERATIVE_MARK = r"(帮我|给我|替我|帮忙|麻烦|我要|我想要|我想)"

_CANCEL_IMPERATIVE_PATTERN = re.compile(
    _IMPERATIVE_MARK
    + r"[^，。？！,?!]{0,4}"
    + r"(取消|撤消|撤销|退掉|退订|作废|退了|不借了|不用了)"
)

# 「把…取消掉」：中文里最典型的祈使取消句式，不带「帮我」也要认出来。
# 惰性匹配 + 12 字窗口，覆盖「把昨天那个预约取消掉」这类带修饰语的表达。
_PUT_CANCEL_PATTERN = re.compile(
    r"把[^，。？！,?!]{0,12}?(取消|撤消|撤销|退掉|退订|作废)(掉|了|吧|它|一下|下)?"
)

_BOOKING_IMPERATIVE_PATTERN = re.compile(
    _IMPERATIVE_MARK + r"[^，。？！,?!]{0,4}(?<![的。])(约|订|定|预约|预订|预定)"
)


def explicit_write_request(text: str) -> str | None:
    """用户是不是在下达写库指令？是的话对应哪个工具？

    返回 "cancel_reservation" / "create_reservation" / None。

    为什么返回工具名而不是 bool：authorized 只能告诉系统「允许写」，
    但写库步骤被模型整步漏掉时，系统得知道该补**哪一个**工具。
    把「补什么」也交给确定性规则，才能彻底避免「用户下了单、系统什么都没做」。

    判定顺序（每一步的顺序都是有理由的，不要随手调换）：

      1. 摘掉只读/情态/讲解/信息名词片段
         「查一下我的预约」里的「预约」是名词，不是动词。
         不先摘掉，读的操作会被当成写的指令 —— 而 cancel_reservation
         不可逆，一次误判就是一条被删掉的预约。
      2. 祈使取消  → 3. 祈使预约
         取消优先：「帮我把周四的取消掉，改约周五下午」同时命中两组动词，
         取取消 —— 不可逆的那一步必须先被看见。
      4. 疑问标记否决
         放在祈使之后，是为了让「…有没有空，有空就帮我预约」这类
         半问半吩咐的句子仍然按吩咐处理。
      5. 光秃秃的动词
         「预约明天下午2点的光学实验室」没有标志词，也是明确指令。
    """
    text = text or ""

    # 1) 先摘掉「读」的片段，再看剩下什么
    payload = _READ_FRAME_PATTERN.sub(_STRIP_FILLER, text)
    payload = _MODAL_FRAME_PATTERN.sub(_STRIP_FILLER, payload)
    payload = _EXPLAIN_FRAME_PATTERN.sub(_STRIP_FILLER, payload)
    payload = _RESERVATION_INFO_PATTERN.sub(_STRIP_FILLER, payload)

    # 2) 祈使取消 —— 不可逆动作优先
    if _CANCEL_IMPERATIVE_PATTERN.search(payload) or _PUT_CANCEL_PATTERN.search(
        payload
    ):
        return "cancel_reservation"

    # 3) 祈使预约
    if _BOOKING_IMPERATIVE_PATTERN.search(payload):
        return "create_reservation"

    # 4) 疑问句不是指令
    if _QUESTION_PATTERN.search(payload):
        return None

    # 5) 没有标志词，只有动词
    if _CANCEL_VERB_PATTERN.search(payload):
        return "cancel_reservation"
    if _BOOKING_VERB_PATTERN.search(payload):
        return "create_reservation"
    return None


def _booking_target_known(slots: dict) -> bool:
    """用户是不是已经指明了「要动哪个实验室 / 哪台设备」。

    为什么不再要求「三要素齐全」：那套判据是显式工作流留下的。当时系统要靠
    resolve_args 自己拼工具参数，要素不齐就没法下单，所以只能等齐了再授权。
    现在参数是模型自己拼的，缺的要素它能用 list_lab_equipments /
    query_lab_availability 现查，或者反过来追问用户 —— 要素齐不齐是**可行性**
    问题，不是**授权**问题。

    而当年真正想挡的是「我打算预约」这种没有对象的句子。所以这里只要求一件事：
    用户提到了实验室名或设备型号，看得见要动的是什么。
    """
    return any(
        slots.get(key) not in (None, "", []) for key in ("lab_name", "equipment_name")
    )


def analyze(ctx: AgentContext, state: AgentState) -> dict:
    """把自然语言解析成 slots + missing_slots + authorized。

    这里刻意**不产出意图**。意图分类是模型能力最不稳的一环，而它对下游也没有
    用处 —— 自主决策环节是拿着用户原话 + 槽位 + 缺失项去挑工具的，多一个
    「意图标签」只是多一次「分类判错、后面全错」的机会。

    留下来的三个产出都是任务状态而不是分类标签：用户说了什么、还缺什么、
    能不能写库。前两个是事实，第三个是安全闸门 —— graph 拿它决定要不要把
    写库工具交给模型。
    """
    query = state.get("user_query") or ""
    today, weekday = today_info(state)

    if breaker_open(ctx):
        logger.info("模型熔断已开启，本轮不再降级，直接告知用户")
        return _unavailable()

    prompt = prompts.ANALYZE_PROMPT.format(
        today=today,
        weekday=weekday,
        history=_history_text(state.get("history")),
        query=query,
    )

    try:
        data = load_json(_ask(ctx, prompt))
    except Exception as exc:  # noqa: BLE001
        # 改造前这里会退回 _fallback_analyze。现在不再兜底：一个拿不到模型
        # 结论的需求理解，产出的槽位和授权结论都不可靠，硬着头皮往下走
        # 只会让用户拿到一份「看着像结果、其实是规则猜的」回答。
        logger.warning("需求解析失败，本轮不再降级：%s", exc)
        return _unavailable()

    slots = data.get("slots") or {}
    if not isinstance(slots, dict):
        slots = {}
    slots = _normalize_slots(slots)

    # 日期是唯一一处**用户原话算得出来、模型却经常算错**的槽位：实测用户接着
    # 说「那后天呢？」，模型沿用了上一轮回复里的「（10月9日）」当成后天 ——
    # 真下单就是一条错日期的记录，而写库不可撤销。日期是纯算术，词面又明确
    # （明天/后天/2026-11-20），所以让确定性抽取当裁判：用户原话里出现了日期
    # 就用它覆盖模型的值；没出现（「那它呢？」）就保持模型/承接的结果不动。
    # ⚠️ 这只是在核一个**事实**，不是用关键词决定调哪个工具。
    spoken_date = _extract_date(query, today)
    if spoken_date and slots.get("date") != spoken_date:
        if slots.get("date"):
            logger.warning(
                "日期以用户原话为准：模型给 %s，原话推出 %s",
                slots["date"],
                spoken_date,
            )
        slots["date"] = spoken_date

    missing = data.get("missing_slots") or []
    if not isinstance(missing, list):
        missing = []

    # 本轮是不是在明确地下单/取消。这个结论后面两处都要用：
    # 一是决定要不要从上文承接槽位，二是 authorized 的确定性兜底。
    wanted = explicit_write_request(query)

    # 模型每轮只看得到这一句话，很容易把「上一轮已经说清楚的实验室」
    # 当成没说过，于是槽位里只剩一个日期。而写库工具（预约 / 取消）
    # 恰恰最依赖这条线索 —— 上一轮回复里写着「实验室：人工智能实验室、
    # 日期：10月9日」，这一轮用户说「取消这个预约」，只捞到日期就会
    # 命中多条、卡在歧义上。所以把上文事实并回槽位。
    # 只在「本轮确实在下单/取消」时承接，免得把很久以前提过的实验室
    # 塞进「今天有哪些开放实验室」这种纯查询里。
    if wanted:
        carried = _carry_over_slots(slots, state.get("history"), today)
        if carried:
            logger.info("本轮是写库请求，沿用上文槽位：%s", "、".join(carried))

    authorized = bool(data.get("authorized"))
    # 模型说「不授权」，但用户原话就是明确的下单/取消指令 —— 以确定性结论为准。
    # 理由见 explicit_write_request 的注释：误判成 false 的代价是
    # 「用户要求的事系统完全没做，模型却可能宣称已办妥」，
    # 而放宽这一次的代价只是「多走一遍 check_user_permission + 时段校验」，
    # 两道校验仍然会挡住真正不该写的请求。
    if not authorized:
        if wanted == "create_reservation" and _booking_target_known(slots):
            logger.warning(
                "模型判定 authorized=false，但用户原话是明确的预约指令且指明了对象，"
                "改用确定性结论放行（否则 %s 会被静默摘除）",
                wanted,
            )
            authorized = True
        elif wanted == "cancel_reservation":
            # 取消不要求三要素齐全：用户说「取消我那条预约」时
            # 本来就不会重复实验室和时间，真正的约束在工具内部
            logger.warning(
                "模型判定 authorized=false，但用户原话是明确的取消指令，"
                "改用确定性结论放行（否则 cancel_reservation 会被静默摘除）"
            )
            authorized = True

    return {
        "slots": slots,
        "missing_slots": [str(item) for item in missing],
        "authorized": authorized,
        "plan_needed": bool(data.get("plan_needed")),
        "reason": str(data.get("reason") or ""),
        "plan_reason": str(data.get("plan_reason") or ""),
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


# 时段词决定「两点」到底是 2 点还是 14 点。
_PERIOD_WORDS = "凌晨|早上|上午|中午|下午|傍晚|晚上"
_CN_NUMBER = r"[零一二两三四五六七八九十]{1,3}"
_HOUR_PART = rf"(?:[0-9]{{1,2}}|{_CN_NUMBER})"
# 分钟要么写成两位数字，要么必须带“分”字 —— 否则「2点到5点」里的 5
# 会被当成「2点」的分钟数。
_MINUTE_PART = rf"(?:[0-9]{{2}}|[0-9]{{1,2}}\s*分|{_CN_NUMBER}\s*分|半)"


# 时间表达正则：抠实验室名之前要先把它抹掉。
# 「下午2点到5点的计算机实验室」如果不先处理，正则会把“点”一样的
# 汉字一起吃掉，抠出「午2点到5点的计算机」这种鬼东西。
_TIME_NOISE = re.compile(
    rf"(?:{_PERIOD_WORDS})?\s*{_HOUR_PART}\s*[:：点时]\s*{_MINUTE_PART}?"
    rf"(?:\s*(?:到|至|~|—|-)\s*(?:{_PERIOD_WORDS})?\s*{_HOUR_PART}"
    rf"\s*[:：点时]\s*{_MINUTE_PART}?)?"
)

# 相对日期词本身也算时间噪声，但不带钟点（「明天的计算机实验室」）。
# 不能写进 _TIME_NOISE：那样会把时间表达中的钟点部分变成可选项，
# 导致「下午两点的计算机实验室」里的“下午”被单独吃掉后剩下一个孤零零的“两”。
_DAY_WORD_NOISE = re.compile(r"(今天|明天|后天|大后天)的?")

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
            # 「帮我把光学实验室那条预约取消掉」会抠出「把光学实验室」：
            # 正则从「帮我」开始贪婪匹配，「帮我」剥掉后「把」就留在名字开头。
            # 「把」当作实验室名首字在中文里没有成立的可能，可以放心剥。
            "把",
            "条",
        ),
        key=len,
        reverse=True,
    )
)


# 提取实验室名时的额外过滤。
# 「现在有哪些开放的实验室？」里「实验室」前面整整 7 个字全是疑问词，正则会
# 整段抠出来当实验室名。问句里提到实验室 ≠ 用户想约它 —— 这种名字一旦进槽位，
# 会被后面的路由当成参数去查一个根本不存在的实验室。
_BOGUS_LAB_MARKERS = (
    "哪些",
    "什么",
    "多少",
    "几个",
    "有没有",
    "是不是",
    "开放",
    "规则",
    "安全",
    "规范",
)


def _extract_lab_name(text: str) -> str | None:
    """从口语里抠出实验室名，例如「明天下午2点的计算机实验室」→ 计算机实验室。"""
    cleaned = _TIME_NOISE.sub(" ", text or "")
    # 相对日期词要先摘掉，否则「明天的计算机实验室」会抠出「明天的计算机实验室」
    cleaned = _DAY_WORD_NOISE.sub(" ", cleaned)
    match = re.search(r"([\u4e00-\u9fa5]{2,8})实验室", cleaned)
    if not match:
        return None

    name = match.group(1)
    if any(marker in name for marker in _BOGUS_LAB_MARKERS):
        return None
    changed = True
    while changed:
        changed = False
        for noise in _LAB_LEAD_NOISE:
            if name.startswith(noise):
                name = name[len(noise) :]
                changed = True
                break
    # 量词不是名字。「帮我约一个实验室」把「帮我」「约」剥掉后只剩「一个」，
    # 补上「实验室」就成了一个不存在的实验室名 —— 实测它会让取消预约
    # 直接失败在「没有找到名为「一个实验室」的实验室」上。
    # 只剥「中文数词 + 个」：「3D 打印实验室」这类以数字开头的真名字不受影响。
    name = re.sub(r"^[一二两三四五六七八九十百千万几]+个", "", name)
    return f"{name}实验室" if len(name) >= 2 else None


def _extract_date(text: str, today: str) -> str | None:
    """从口语里抠出日期，例如「明天」「后天」「10 月 9 日」→ YYYY-MM-DD。"""
    base = datetime.strptime(today, "%Y-%m-%d")
    if "后天" in text:
        return (base + timedelta(days=2)).strftime("%Y-%m-%d")
    if "明天" in text:
        return (base + timedelta(days=1)).strftime("%Y-%m-%d")
    if "今天" in text:
        return today
    matched = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if matched:
        return (
            f"{matched.group(1)}-{int(matched.group(2)):02d}-"
            f"{int(matched.group(3)):02d}"
        )
    # 「2026年10月9日」：助手在回复里回述日期时就长这样。不认这种写法，
    # 承接上文时就会把日期漏掉（用户自己那句话里往往根本没有日期）。
    matched = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text)
    if matched:
        return (
            f"{matched.group(1)}-{int(matched.group(2)):02d}-"
            f"{int(matched.group(3)):02d}"
        )
    return None


# 兜底解析能往回看几轮用户发言。3 轮足以覆盖「先报实验室 → 再补日期 → 再补时间」
# 这种最常见的节奏，又不会把很久以前提过的实验室莫名其妙套到当下的问题上。
_CARRY_OVER_TURNS = 3


def _carry_over_slots(slots: dict, history: list[dict] | None, today: str) -> list[str]:
    """把前几轮已经交代过的关键信息接到本轮槽位上，返回沿用了哪些字段。

    为什么必须有这一步：解析只看得到本轮这一句话。用户先问
    「帮我预约计算机实验室」，再补一句「明天下午两点」——这句话里根本没有
    实验室名，就会反过来追问「还需要你补充：实验室」，用户看到的结论是
    「这东西失忆了」。而兜底恰恰是模型限流/熔断时用户最常遇到的路径。

    ⚠️ **助手说过的话同样算数，而且往往才是唯一记载。**
    一桩已经办完的事，「是哪个实验室、哪一天」通常只写在助手那条确认回复里
    （「实验室：人工智能实验室 / 日期：2026年10月9日」），
    用户自己那句话里反而没有（他只说了「帮我约一个有 RTX5090 的实验室」，
    实验室是查出来的）。只扫用户发言，等于在最关键的一步把线索丢掉：
    用户接着说「取消这个预约」时，槽位里只剩一个日期，而日期恰恰命中多条
    待审核预约，取消就卡在「无法确定要取消哪一条」上。

    只接实验室名和日期：这两个是判定「能不能下单」必需的事实，
    而具体时段最容易反复修改 —— 宁可多问一句，也不要拿旧时间真的下单。
    """
    if not history:
        return []

    texts = [
        (item.get("content") or "").strip()
        for item in history[-(_CARRY_OVER_TURNS * 2) :]
        if (item.get("role") or "") in ("user", "assistant")
    ]
    texts = [text for text in texts if text]
    if not texts:
        return []

    carried: list[str] = []
    for key in ("lab_name", "date"):
        if slots.get(key):
            continue
        # 从最近的发言往前找：越近越可能是用户正在说的那件事
        for text in reversed(texts):
            if key == "lab_name":
                value = _extract_lab_name(text)
                if value and any(mark in value for mark in _BOGUS_LAB_MARKERS):
                    value = None
            else:
                value = _extract_date(text, today)
            if value:
                slots[key] = value
                carried.append(key)
                break
    return carried


# ---------------------------------------------------------------------------
# 节点 1.5：任务规划（Plan-and-Execute，条件触发）
#
# 只有在 analyze 判定 plan_needed=True 时才会走到这里。快路径上这两个函数
# 一次都不会被调用 —— 规划能力是按需买的，不是每轮都付。
# ---------------------------------------------------------------------------


def plan_steps_text(plan: list[dict[str, Any]] | None) -> str:
    """把计划渲染成一段给**提示词/轨迹**看的中文，步骤编号从 1 开始。"""
    lines: list[str] = []
    for index, step in enumerate(plan or [], start=1):
        goal = (step.get("goal") or "").strip()
        hint = step.get("hint_tools") or []
        hint_text = "、".join(hint) if hint else "不限"
        lines.append(f"{index}. {goal}（可用工具：{hint_text}）")
    return "\n".join(lines)


def step_context_text(state: AgentState) -> str:
    """当前步骤的一句话上下文，注入 AGENT_SYSTEM_PROMPT 的 {step_context}。

    这是规划层和执行层之间**唯一**的信息通道：executor 不需要知道「计划从哪来」，
    它只要知道「我现在在做第几步、这一步要达成什么、能用哪些工具」。
    这也是 executor 能完全复用原有 ReAct 循环的原因 —— 计划只影响「给它什么
    约束」，不影响「它怎么跑」。

    hint_tools 在这里是**硬约束**而不是建议：node_execute 会拒绝清单外的调用。
    措辞必须与之一致，否则模型会去点一个注定被拒的工具，白烧一轮预算。
    """
    plan = state.get("plan") or []
    if not plan:
        return "（本轮未启用任务计划，直接完成用户的请求）"
    cursor = state.get("plan_cursor") or 0
    total = len(plan)
    if cursor >= total:
        return "（任务计划的全部步骤已完成，请给出面向用户的最终回复）"
    step = plan[cursor] or {}
    goal = (step.get("goal") or "").strip()
    hint = step.get("hint_tools") or []
    hint_text = (
        f"本步只能使用这些工具：{'、'.join(hint)}；不要调用清单外的工具"
        if hint
        else "本步不限制工具"
    )
    if cursor + 1 >= total:
        tail = "这是**最后一步**，请给出面向用户的最终回复。"
    else:
        tail = f"完成本步后还剩 {total - cursor - 1} 步。"
    return f"第 {cursor + 1}/{total} 步：{goal}（{hint_text}）。{tail}"


def _normalize_plan(raw: Any, authorized: bool) -> list[dict[str, Any]]:
    """把模型吐出的步骤列表清洗成可执行的计划。

    清洗的四件事，每一件都是在堵一条「模型不老实」的路：
      1. **裁到 MAX_PLAN_STEPS**：模型很爱一口气排 8 步，超出的部分只会
         让工具预算被摊薄。
      2. **丢掉不认识的工具名**：hint_tools 里的名字若不在 TOOL_REGISTRY 里，
         留着只会在收窄时被静默丢弃（或者更糟 —— 让计划看起来覆盖了某个
         能力其实没有）。这里直接删掉，让 hint_tools 与实际能力一致。
      3. **未授权时剥掉 WRITE_TOOLS**：镜像 bindable_tools 的语义。计划里
         写着 create_reservation、实际 bind_tools 里没有它，模型会反复尝试
         一个摸不到的工具 —— 提示词和实际能力必须描述同一件事。
      4. **重排编号**：模型给的编号可能乱、可能重复；统一改成 1..n，
         顺便把 depends_on 里越界的编号清掉（只能引用更小的步骤号）。
    """
    if not isinstance(raw, list):
        return []

    plan: list[dict[str, Any]] = []
    for item in raw[:MAX_PLAN_STEPS]:
        if not isinstance(item, dict):
            continue
        goal = str(item.get("goal") or "").strip()
        if not goal:
            # 没有目标的一步等于没有这一步 —— 留着会让 executor 空转一轮。
            continue

        hints: list[str] = []
        raw_hints = item.get("hint_tools")
        if isinstance(raw_hints, list):
            for name in raw_hints:
                key = str(name).strip()
                if key not in tools.TOOL_REGISTRY:
                    continue
                if key in WRITE_TOOLS and not authorized:
                    continue
                if key not in hints:
                    hints.append(key)

        depends: list[int] = []
        raw_depends = item.get("depends_on")
        if isinstance(raw_depends, list):
            for value in raw_depends:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                # 只能依赖更靠前的步骤（编号 1 基，当前步是 len(plan)+1）
                if 1 <= number <= len(plan):
                    depends.append(number)

        plan.append(
            {
                "step": len(plan) + 1,
                "goal": goal,
                "hint_tools": hints,
                "depends_on": depends,
            }
        )
    return plan


def _plan_unavailable() -> dict:
    """规划失败时的产出：**不置 error**，而是给出空计划。

    这里和 analyze 的处理刻意不同：analyze 拿不到结论就诚实告知用户，
    因为槽位和授权都不可靠，硬走只会骗人。而规划失败不影响「能不能答」——
    它只影响「按不按计划答」。空计划会让 _after_plan 把这一轮送回
    原有的 agent ⇄ execute 快路径 —— 规划层没生成出来，就退化成今天的行为，
    这比停下来报错对用户更有用。
    """
    return {"plan": [], "plan_revision": 0, "step_results": []}


def make_plan(ctx: AgentContext, state: AgentState) -> dict:
    """拆解任务计划。同步调用（复用 _ask，继承重试 / 429 / 熔断语义）。

    为什么规划是同步的而 agent_step 是异步的：规划只需要一段 JSON 文本，
    _ask 已经把这套同步重试逻辑写好了；而 agent_step 要拿回带 tool_calls 的
    完整 AIMessage，必须用异步的 _ainvoke。复用现成的 _ask 比再造一个异步
    同胞更省事，也不会让两套重试策略漂移。
    """
    if breaker_open(ctx):
        logger.info("模型熔断已开启，本轮不做规划，退回单步路径")
        return _plan_unavailable()

    today, weekday = today_info(state)
    authorized = bool(state.get("authorized"))
    prompt = prompts.PLAN_PROMPT.format(
        query=state.get("user_query") or "",
        today=today,
        weekday=weekday,
        slots=_slots_text(state.get("slots") or {}),
        missing_slots=describe_missing(state.get("missing_slots")) or "（无）",
        memory=state.get("memory_context") or "（暂无）",
        tools=tools.describe_tools(authorized),
        max_steps=MAX_PLAN_STEPS,
        permission=(
            prompts.PERMISSION_GRANTED if authorized else prompts.PERMISSION_DENIED
        ),
    )

    try:
        data = load_json(_ask(ctx, prompt))
    except Exception as exc:  # noqa: BLE001
        logger.warning("任务规划失败，退回单步路径：%s", exc)
        return _plan_unavailable()

    plan = _normalize_plan(data.get("steps"), authorized)
    if not plan:
        logger.warning("模型没给出可用的计划步骤，退回单步路径")
        return _plan_unavailable()

    logger.info("已生成任务计划（%s 步）：%s", len(plan), plan_steps_text(plan))
    return {
        "plan": plan,
        "plan_revision": 0,
        "step_results": [],
        "plan_reason": str(data.get("reason") or ""),
    }


def revise_plan(ctx: AgentContext, state: AgentState) -> dict:
    """按已拿到的观察结果重排剩余步骤。

    调用它的前提是 graph._replan_needed() 已经确认「有失败步骤、且有可行的
    替代工具」—— 也就是说，这个函数**不会**在步骤都跑对的时候被调到。
    v1 的教训（docs/agent-architecture.md §7.6）就是「步骤全成功后还重规划，
    把答对的题改错了」，那条闸门就是为此设的。

    拼接方式：plan = 已走完的前缀 + 模型新给的剩余步骤。
    plan_cursor 保持在原处 —— 它正好指向新计划的第一步。
    """
    if breaker_open(ctx):
        logger.info("模型熔断已开启，放弃重规划")
        return {"plan_revision": (state.get("plan_revision") or 0) + 1}

    cursor = state.get("plan_cursor") or 0
    plan = list(state.get("plan") or [])
    revision = state.get("plan_revision") or 0
    authorized = bool(state.get("authorized"))

    # 出问题的那一步 + 它拿到的观察结果。给模型看的是**观察结果**而不是
    # 「失败」这个标签 —— 它需要据此判断「是参数不对、还是这个方向根本不通」。
    failed_step = plan[cursor] if cursor < len(plan) else None
    current = [
        item for item in (state.get("tool_results") or []) if item.get("step") == cursor
    ]
    observations = (
        "\n".join(
            tools.summarize(item.get("tool") or "", item.get("result") or {})
            for item in current
        )
        or "（这一步没有拿到任何工具结果）"
    )

    prompt = prompts.REPLAN_PROMPT.format(
        query=state.get("user_query") or "",
        slots=_slots_text(state.get("slots") or {}),
        done_steps=plan_steps_text(plan[:cursor]) or "（还没有完成的步骤）",
        failed_step=plan_steps_text([failed_step] if failed_step else []) or "（无）",
        observations=observations,
        tools=tools.describe_tools(authorized),
        max_steps=MAX_PLAN_STEPS,
    )

    try:
        data = load_json(_ask(ctx, prompt))
    except Exception as exc:  # noqa: BLE001
        logger.warning("重规划失败，保持原计划继续：%s", exc)
        return {"plan_revision": revision + 1}

    remaining = _normalize_plan(data.get("steps"), authorized)
    if not remaining:
        logger.warning("重规划没给出可用步骤，保持原计划继续")
        return {"plan_revision": revision + 1}

    new_plan = plan[:cursor] + remaining
    # 拼接后必须**重新编号**：remaining 里的 step 是模型按「剩余步骤」自己从 1 数起的，
    # 直接拼上去会得到 1, 1, 2, 3 这种重复编号。前端靠下标渲染「第几步」，
    # 编号一乱就对不上了。depends_on 同理要整体平移 cursor —— 模型写的是
    # 「我这份清单里的第几个」，拼进来之后它的基准变成了全局序号。
    for index, item in enumerate(new_plan, start=1):
        item["step"] = index
        if index > cursor:
            item["depends_on"] = [d + cursor for d in item.get("depends_on") or []]
    logger.info(
        "已重排计划（第 %s 次修订）：%s", revision + 1, plan_steps_text(remaining)
    )
    return {
        "plan": new_plan,
        "plan_revision": revision + 1,
        "step_round": 0,
        "plan_reason": str(data.get("reason") or ""),
    }


# ---------------------------------------------------------------------------
# 节点 2：自主决策
# ---------------------------------------------------------------------------


async def _ainvoke(ctx: AgentContext, llm: Any, messages: list) -> Any:
    """_ask 的异步同胞：同一套重试 / 熔断 / 限流策略，但返回完整的消息。

    为什么不复用 _ask：结构化的那两个环节只想要 content 里的 JSON，
    而自主决策要的是 tool_calls —— 它是 AIMessage 上另一个平级字段，
    把消息压成字符串就再也拼不回来了。

    重试与熔断的记账方式和 _ask 完全一致：只有「一次完整的询问终于成功 /
    彻底放弃」才 _record，单次尝试的失败不记账（否则一次瞬时抖动就能把
    熔断打开）。429 照样不重试 —— 重试只会更快烧掉配额。
    """
    last: Exception | None = None
    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        if delay:
            await asyncio.sleep(delay)
        started = time.monotonic()
        try:
            message = await llm.ainvoke(messages)
        except Exception as exc:
            cause = getattr(exc, "__cause__", None) or exc
            if not is_transient(cause) or too_slow_to_retry(started):
                _record(ctx, ok=False)
                if is_rate_limited(cause):
                    logger.warning("模型配额已用尽（429），本轮其余环节不再请求模型")
                raise
            last = exc
            logger.warning("模型调用第 %s 次失败，稍后重试：%s", attempt, cause)
        else:
            _record(ctx, ok=True)
            return message
    _record(ctx, ok=False)
    raise last  # type: ignore[misc]


def build_agent_messages(state: AgentState) -> list[AnyMessage]:
    """组装自主决策环节的消息列表。

    顺序是刻意的：系统提示词 → 历史（背景）→ 本轮问题 → 已有的工具往返记录。

    历史必须放在「本轮问题」**之前**，而且必须写明它只是背景。实测把历史
    直接拼进系统提示词时，模型会把上一轮那句「帮我约一个有 RTX5090 的实验室」
    当成当前问题再答一遍 —— 它看到最近的一条 user 消息，就当成问题。

    末尾的 messages 是本轮已经发生的工具往返（AIMessage 的 tool_calls +
    对应的 ToolMessage）。它就是 ReAct 循环里的「短期记忆」，这一段完全由
    graph 在每轮工具执行后追加，planner 不自己维护。
    """
    today, weekday = today_info(state)
    messages: list[AnyMessage] = [
        SystemMessage(
            content=prompts.AGENT_SYSTEM_PROMPT.format(
                today=today,
                weekday=weekday,
                query=state.get("user_query") or "",
                slots=_slots_text(state.get("slots") or {}),
                # 给中文描述而不是字段名：模型看到 `date` 会照抄进回复，
                # 看到「预约日期」才会用中文问用户。
                missing_slots=describe_missing(state.get("missing_slots")) or "（无）",
                memory=state.get("memory_context") or "（暂无）",
                # 规划层与执行层之间唯一的信息通道：本步要做什么、建议用什么工具。
                # 快路径下它渲染成一句「本轮未启用任务计划」。
                step_context=step_context_text(state),
                permission=(
                    prompts.PERMISSION_GRANTED
                    if state.get("authorized")
                    else prompts.PERMISSION_DENIED
                ),
            )
        )
    ]

    history = state.get("history") or []
    if history:
        messages.append(
            HumanMessage(
                content=(
                    "以下是之前的对话记录，只用于理解指代和上下文，"
                    "**不是**本次要回答的问题：\n" + _history_text(history)
                )
            )
        )

    messages.append(HumanMessage(content=state.get("user_query") or ""))
    messages.extend(state.get("messages") or [])
    return messages


async def agent_step(
    ctx: AgentContext,
    state: AgentState,
    *,
    tool_choice: str | None = None,
    hint_tools: set[str] | None = None,
) -> AIMessage:
    """自主决策：由模型决定这一步调哪个工具、带什么参数。

    这是本次改造的核心。旧实现里模型只能吐一份 JSON 计划，之后还要经过
    参数补全、白名单过滤、补写库步骤三道加工 —— 模型「想调什么」和工具
    真正拿到的参数是两件事，中间任何一环出错都会表现为「查到的和答的
    对不上」。现在模型的 tool_calls 就是调用指令本身，没有第二个经手人。

    工具清单由 tools.bindable_tools(authorized) 决定：没拿到写库授权时，
    写库工具**根本不在清单里** —— 模型不是「被劝阻」，而是无处可调。
    这比旧实现里 _sanitize_plan 事后把写库步骤摘掉更彻底：摘掉之前，
    模型已经按「计划里能写库」的语气组织过它的叙述了。

    hint_tools 是规划层给的**逐步收窄**：执行计划第 N 步时只把该步点名的
    工具交给模型。收窄是在 bindable_tools 内部与授权结果求交的，
    所以它无法绕开写库闸门；交集为空时 bindable_tools 会退回全集。

    tool_choice 只在轮次用尽时被置为 "none"，逼模型基于已有观察给结论。
    """
    llm = get_llm()
    llm = llm.bind_tools(
        tools.bindable_tools(bool(state.get("authorized")), only=hint_tools),
        tool_choice=tool_choice,
    )

    message = await _ainvoke(ctx, llm, build_agent_messages(state))
    if not isinstance(message, AIMessage):
        # 理论上不会发生（ainvoke 返回的就是 AIMessage），
        # 但让更上层拿到一个「不是消息的东西」排查成本太高，不如在这里断掉。
        raise BusinessException(message=MODEL_UNAVAILABLE)
    return message
