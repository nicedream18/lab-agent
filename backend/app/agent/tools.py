"""Agent 的工具层。

设计原则：**LLM 永远不直接碰数据库**。
所有业务能力都封装成这里注册的函数，LLM 只能「申请调用哪个工具、传什么参数」，
真正的数据库读写全部发生在受控的 Python 代码里。

这样做的三个好处：
1. 安全 —— LLM 无法构造任意 SQL，也无法伪造 user_id 越权操作别人的预约；
2. 可控 —— 参数在进入业务逻辑前会先过白名单和类型校验；
3. 可测 —— 每个工具都能脱离 LLM 单独做单元测试。

改造说明（v3，LangChain 标准 @tool）：
每个工具都是标准的 langchain_core.tools.@tool 对象：描述就是 @tool 的 description，
参数 schema 由函数签名 + Annotated[..., Field(description=...)] 自动推断，
bindable_tools() 直接把 BaseTool 列表交给 ChatOpenAI.bind_tools()。

三个曾经的顾虑现在各有落点：
  1. handler 的第一个参数是 AgentContext（数据库会话 + 用户身份）。
     用 Annotated[AgentContext, InjectedToolArg] 标成注入参数后，
     convert_to_openai_tool() 会把它从**发给模型的 schema**（tool_call_schema）里摘掉，
     模型看不到、也编不出来；执行时由 run_tool() 手动注入。
     ⚠️ 但 InjectedToolArg 只影响发给模型的那份 schema，pydantic 的 args_schema
     仍然把 ctx 当成必填参数，所以执行必须走 tool.func(ctx, **args)，
     不能用 tool.invoke()。
  2. 工具失败必须返回 {"ok": False, "error": ...} 而不是抛异常，
     因为对 Agent 来说失败是一个可阅读的观察结果。这一层由 run_tool() 统一兜住，
     所以工具内部可以放心 raise BusinessException。
  3. 中文展示名 label 放在 @tool(extras={"label": ...}) 里；
     TOOL_REGISTRY（被 /api/agent/tools、tracer 和 run_tool 消费）由 @register
     叠在 @tool 外面登记，定义顺序即前端展示顺序。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Annotated, Any

from langchain_core.tools import BaseTool, InjectedToolArg, StructuredTool, tool
from pydantic import Field

from app.agent.state import WRITE_TOOLS, AgentContext
from app.common.exceptions import BusinessException
from app.models.equipment import Equipment
from app.models.lab import Lab
from app.models.reservation import Reservation
from app.schemas.reservation import ReservationCreateRequest
from app.services import (
    equipment_service,
    kb_service,
    lab_service,
    reservation_service,
)

# 单个工具返回给模型的最大字符数。不做限制的话，
# 一次实验室列表查询就能把上下文撑爆，且浪费 token。
# ⚠️ 这个值和 kb_service.TOP_K_FINAL 必须一起看：
# 知识库检索会返回 TOP_K_FINAL(3) 个切片，每片正文 ≤ 300 字 + 章节路径前缀，
# 经过 json.dumps（换行要转义成 \n）后大约 1100~1300 字。
# 原来的 1500 只剩 15% 余量，稍长的切片就会被「…（已截断）」砍在句子中间，
# 而且这里和 kb_service 的两处截断互相不知情 —— 所以留足余量。
MAX_RESULT_CHARS = 2400

# 用户名下「待审核 + 已通过」预约的配额上限
MAX_ACTIVE_RESERVATIONS = 5

# 预约状态码 → 中文。表里存的是 0/1/2/3，但模型和用户看到的都必须是中文，
# 否则「status: 0」出现在 observations 里，模型很可能会把它当成失败。
RESERVATION_STATUS_TEXT = {0: "待审核", 1: "已通过", 2: "已拒绝", 3: "已取消"}

# 只有「待审核」的预约允许被用户取消（与 service 层的规则一致）。
# 在这里单独取个名字，是为了让工具能**提前**判断可取消性并给出好文案，
# 而不是等 service 抛一个「当前状态无法取消」再转发给用户。
CANCELABLE_STATUS = 0


# 工具注册表：工具名 -> BaseTool。
#
# 保留这张表是因为 /api/agent/tools、tracer 和 run_tool 都按名字取工具，
# 而 dict 的插入顺序稳定 —— 前端「工具箱」面板的排序因此不会抖动。
#
# 值类型取 BaseTool 而非 StructuredTool：@tool 的存根把返回值标成 BaseTool
# （只有运行时才真的是 StructuredTool），声明取基类才能同时容纳装饰器产物
# 和 model_copy() 出来的副本。
TOOL_REGISTRY: dict[str, BaseTool] = {}


def register(tool_obj: BaseTool) -> BaseTool:
    """把 @tool 产出的工具登记进 TOOL_REGISTRY。

    用法是叠在 @tool 外面::

        @register
        @tool(description="...", extras={"label": "..."})
        def my_tool(...): ...

    这样描述 / label / 参数 schema 都只有一份定义，
    不会再出现「改了一处忘了改另一处」。
    """
    TOOL_REGISTRY[tool_obj.name] = tool_obj
    return tool_obj


def bindable_tools(authorized: bool, only: set[str] | None = None) -> list[BaseTool]:
    """挑出本轮可以绑定给模型的工具，供 llm.bind_tools() 使用。

    这里返回的是 BaseTool 对象而不是手拼的 dict —— bind_tools() 两种都收，
    而 BaseTool 能保证「发给模型的 schema」和「工具自己的签名」永远一致。

    authorized 是写库闸门在「能力层」的落点：
    未授权时写库工具会**从清单里消失**，模型看不到、也调不到。
    这比在提示词里写「不许调用」要强得多 —— 提示词是模型可以选择不听的话，
    没提供的工具是它真的拿不到。

    only 是 v3 规划层追加的**逐步收窄**：执行计划的第 N 步时，
    只把该步 hint_tools 里点名的工具交给模型，让它专注。
    两个注意点：
      1. 收窄是「与授权结果求交」而不是「只按 only 取」，
         所以它**不可能**把写库工具绕开授权拉回来；
      2. 交集为空时退回未收窄的清单 —— 计划里写错了工具名（或某个
         工具本轮被授权闸门拿掉了）不该让模型手里一个工具都没有、
         那一步只能空转。宁可不收窄，也不要饿死。

    写库工具的描述会加 "[写库] " 前缀：让模型在扫清单时就能识别哪些调用会改数据，
    而不是等读完一整段描述才发现。与 AGENT_SYSTEM_PROMPT 里的
    「写库工具（会标 [写库]）」一一对应。
    BaseTool 是 pydantic 模型，model_copy 只改副本，不影响 TOOL_REGISTRY 里的原件。
    """
    items: list[BaseTool] = []
    for tool_obj in TOOL_REGISTRY.values():
        if tool_obj.name in WRITE_TOOLS:
            if not authorized:
                continue
            tool_obj = tool_obj.model_copy(
                update={"description": f"[写库] {tool_obj.description}"}
            )
        items.append(tool_obj)

    if only:
        narrowed = [item for item in items if item.name in only]
        # 交集为空就放弃收窄（而不是返回空清单）—— 见上面第 2 点。
        if narrowed:
            return narrowed
    return items


def bindable_tool_names(authorized: bool, only: set[str] | None = None) -> list[str]:
    """本轮真正交给模型的工具名清单。

    node_agent 需要它把「这一轮我提供了哪些工具」告诉执行节点，执行节点据此
    拒绝越界调用。必须复用 bindable_tools 而不是自己按 only 过滤 ——
    收窄有「交集为空就退回全集」的兜底，自己算会算出与实际绑定不一致的清单，
    那样兜底路径下的合法调用会被误拒。
    """
    return [item.name for item in bindable_tools(authorized, only=only)]


def describe_tools(authorized: bool) -> str:
    """把本轮可用工具渲染成一段给**规划提示词**看的文字。

    为什么必须复用 bindable_tools() 而不是直接遍历 TOOL_REGISTRY：
    规划层会拿着这份清单去挑 hint_tools，而 hint_tools 最终又要喂回
    bindable_tools()。两边必须描述同一件事 —— 如果这里列了未授权的写库工具，
    计划里就会出现一个模型永远调不到的名字（然后在收窄时被静默丢掉）。
    用同一个函数产出，这两个清单就不可能不一致。

    格式：`名称 — 中文名 — 说明（写库/只读）`，一行一个。
    """
    lines: list[str] = []
    for tool_obj in bindable_tools(authorized):
        kind = "写库" if tool_obj.name in WRITE_TOOLS else "只读"
        description = (tool_obj.description or "").strip().replace("\n", " ")
        lines.append(
            f"- {tool_obj.name} — {tool_label(tool_obj.name)} — "
            f"{description}（{kind}）"
        )
    return "\n".join(lines) if lines else "（本轮没有可用工具）"


def is_duplicate_write(
    name: str, args: dict[str, Any], results: list[dict[str, Any]] | None
) -> bool:
    """这个写库调用是不是「之前已经成功做过一次」的重复提交？

    只对写库工具为真有意义（调用方先判 name in WRITE_TOOLS）。

    为什么需要它：重规划（replan）会拿到「已完成的步骤」重新拆解，而模型
    很可能把「已经成功的那次创建」再安排一遍。写库不可撤销，重复创建就是
    两条真实数据。所以执行层做一道幂等闸门：同名 + 同参数 + 之前 ok 为真
    → 判为重复，跳过。

    为什么按参数比而不是按「做过这个工具」：同一个工具用**不同**参数调用
    是正常的（比如先查 A 实验室再查 B），只有一模一样的参数才是重复。
    """
    if name not in WRITE_TOOLS:
        return False
    for item in results or []:
        if item.get("tool") != name or not item.get("ok"):
            continue
        if (item.get("args") or {}) == (args or {}):
            return True
    return False


def tool_label(name: str) -> str:
    """工具的中文展示名。

    @tool 没有 label 字段，所以约定放在 extras={"label": ...} 里；
    缺失时退化成工具名，绝不抛异常 —— 它只用于展示，不该拖垮主流程。
    """
    tool_obj = TOOL_REGISTRY.get(name)
    if tool_obj is None:
        return name
    # extras 在存根里是可空的，兜一下底：label 缺失本来就该退化成工具名。
    return (tool_obj.extras or {}).get("label") or name


def run_tool(ctx: AgentContext, name: str, args: dict[str, Any]) -> dict:
    """执行工具，把异常统一收敛成 {"ok": False, "error": ...}。

    这里**必须**吞掉异常而不是往外抛：工具失败对 Agent 来说是
    「一个可反思的观察结果」，而不是「整个流程崩掉」。
    """
    tool_obj = TOOL_REGISTRY.get(name)
    if tool_obj is None:
        return {"ok": False, "error": f"工具 {name} 不存在"}

    # 注册表按 BaseTool 存，但 .func 只有 StructuredTool 才有。
    # 这里收窄一次，后面才能安全地手动注入 ctx；顺手把 func 可能为 None
    # 这条运行时分支也补上 —— 类型检查器抱怨的其实是真事。
    if not isinstance(tool_obj, StructuredTool):
        return {"ok": False, "error": f"工具 {name} 不是可调用的结构化工具"}
    func = tool_obj.func
    if func is None:
        return {"ok": False, "error": f"工具 {name} 没有可调用的实现"}

    # 过掉 args 里多余的键：模型经常多塞几个没定义的参数，
    # 直接 **args 会 TypeError，所以要按参数定义做白名单过滤。
    # 白名单取自 tool_call_schema（已排除注入的 ctx），
    # 用 args_schema 反而会把 ctx 也算成「模型可以传的参数」。
    # 顺手丢掉 None / "" / [] 的占位参数：模型爱把可选参数写成 null，
    # 留着既污染 trace 里的调用记录，又会盖掉函数本身的默认值。
    schema = tool_obj.tool_call_schema
    if isinstance(schema, dict):
        # 分支兜底：args_schema 传了裸 dict 时才走这里
        allowed = set(schema.get("properties") or {})
    else:
        # pydantic 的 model_fields 是类描述符，存根里看不到，运行时必然存在
        allowed = set(getattr(schema, "model_fields", {}))
    clean = {
        key: value
        for key, value in (args or {}).items()
        if key in allowed and value not in (None, "", [])
    }

    try:
        # 必须走 .func 手动注入 ctx：InjectedToolArg 只把 ctx 从发给模型的 schema
        # 里摘掉了，pydantic 的 args_schema 仍要求传它，所以 tool.invoke({...})
        # 会因为缺 ctx 直接抛 ValidationError。
        payload = func(ctx, **clean)
        payload.setdefault("ok", True)
        return payload
    except BusinessException as exc:
        return {"ok": False, "error": exc.message}
    except TypeError as exc:
        # 参数缺失/类型不对，是模型填参的锅，属于可反思错误
        return {"ok": False, "error": f"参数不正确：{exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"工具执行失败：{exc}"}


def summarize(name: str, result: dict) -> str:
    """把工具结果压成一句人话，用于 observations 和前端过程展示。"""
    if result.get("ok") is False:
        return f"{tool_label(name)} 失败：{result.get('error')}"
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "…（已截断）"
    return f"{tool_label(name)} 返回：{text}"


# ---------------------------------------------------------------------------
# 内部解析辅助
# ---------------------------------------------------------------------------


def _find_lab(ctx: AgentContext, lab_name: str) -> Lab | None:
    """按名称找实验室：先精确、再模糊。

    精确优先很重要 —— 库里有「光学实验室」和「光学实验室(分部)」时，
    用户说「光学实验室」应该命中前者。
    """
    if not lab_name:
        return None
    name = lab_name.strip()
    lab = ctx.db.query(Lab).filter(Lab.name == name).first()
    if lab:
        return lab
    return (
        ctx.db.query(Lab).filter(Lab.name.ilike(f"%{name}%")).order_by(Lab.id).first()
    )


def _find_equipment(
    ctx: AgentContext, lab_id: int, equipment_name: str
) -> Equipment | None:
    if not equipment_name or not lab_id:
        return None
    name = equipment_name.strip()
    query = ctx.db.query(Equipment).filter(Equipment.lab_id == lab_id)
    return (
        query.filter(Equipment.name == name).first()
        or query.filter(Equipment.name.ilike(f"%{name}%")).first()
    )


def _require_lab(ctx: AgentContext, lab_name: str) -> Lab:
    # 空名称要单独报错。不然后面会拼出「没有找到名为「None」的实验室」,
    # 把“参数丢了”伪装成“实验室不存在”，排查时非常浪费时间。
    if not (lab_name or "").strip():
        raise BusinessException(message="缺少实验室名称，无法执行该操作")
    lab = _find_lab(ctx, lab_name)
    if lab is None:
        raise BusinessException(message=f"没有找到名为「{lab_name}」的实验室")
    return lab


def _as_int(value: Any) -> int | None:
    """把模型可能送来的 "3" / 3.0 / "前3条" 统一成 int，认不出来返回 None。

    为什么需要它：模型填数字参数时经常带引号或量词（"3"、"3条"），
    直接 int() 会抛 ValueError，run_tool 会把它变成一条含糊的
    「参数不正确」错误 —— 排查时会以为是工具签名的问题，
    而真正的原因是模型把数字写成了字符串。这里直接归一，
    让工具只看得到规范值。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉，避免 True→1
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        digits = re.search(r"-?\d+", str(value))
        return int(digits.group()) if digits else None


def _lab_name_of(item: Any) -> str | None:
    """取预约记录里的实验室名，兼容两种形态。

    为什么需要兼容：service 层返回的**不是** ORM 行，而是
    ReservationResponse（实验室名已经展平成 lab_name）。
    工具如果把两种形态混着用，就会出现「查到了数据却报
    object has no attribute 'lab'」这种自相矛盾的失败 ——
    排查时很容易误以为是查询没命中。
    """
    flat = getattr(item, "lab_name", None)
    if flat:
        return flat
    lab = getattr(item, "lab", None)
    return getattr(lab, "name", None) if lab else None


def _equipment_name_of(item: Any) -> str | None:
    """同 _lab_name_of，取设备名。"""
    flat = getattr(item, "equipment_name", None)
    if flat:
        return flat
    equipment = getattr(item, "equipment", None)
    return getattr(equipment, "name", None) if equipment else None


def _reservation_brief(item: Any) -> dict:
    """把一条预约压成模型和前端都好看的短字典。

    入参可能是 ORM 行，也可能是 ReservationResponse，两者都要能处理。
    """
    return {
        "reservation_id": item.id,
        "lab_name": _lab_name_of(item),
        "equipment_name": _equipment_name_of(item),
        "date": item.date,
        "start_time": item.start_time,
        "end_time": item.end_time,
        "status": item.status,
        "status_text": RESERVATION_STATUS_TEXT.get(item.status, "未知"),
        "cancelable": item.status == CANCELABLE_STATUS,
    }


# ---------------------------------------------------------------------------
# 工具 1：知识库检索
# ---------------------------------------------------------------------------


@register
@tool(
    description=(
        "查询实验室的规章制度、安全规范、开放时间等制度性资料。"
        "问「规则」「能不能带食物」「安全要求」时用它。"
    ),
    extras={"label": "检索实验室知识库"},
)
def search_lab_docs(
    _: Annotated[AgentContext, InjectedToolArg],  # 注入的上下文，本工具用不到
    query: Annotated[str, Field(description="检索关键词")],
) -> dict:
    try:
        content = kb_service.search(query or "")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"知识库检索失败：{exc}"}
    if not content:
        return {"ok": True, "found": False, "content": "知识库里没有相关资料"}
    return {"ok": True, "found": True, "content": content}


# ---------------------------------------------------------------------------
# 工具 2：开放实验室列表
# ---------------------------------------------------------------------------


@register
@tool(
    description=(
        "列出所有开放中的实验室（含 id、名称、位置、容量、开放时间）。"
        "用户没给准确实验室名时先用它确认。"
    ),
    extras={"label": "查询开放实验室"},
)
def list_open_labs(
    ctx: Annotated[AgentContext, InjectedToolArg],
    keywords: Annotated[str, Field(description="名称关键词，可为空")] = "",
) -> dict:
    keyword = (keywords or "").strip() or None
    page = _lab_page(ctx, keyword)
    keyword_matched = True
    if keyword and page.total == 0:
        # 关键词一个都没命中 —— 退回「不带过滤」再查一次。
        #
        # 为什么要这么做：模型很爱把问句里的修饰语当关键词塞进来
        # （实测「现在有哪些实验室开放？」→ keywords="实验室开放"）。
        # 严格按过滤执行就会得到空列表，然后 Agent 会**如实**汇报
        # 「当前没有任何实验室开放」—— 把模型的措辞失误放大成了一条
        # 与事实相反的业务结论。返回多几行只是不够精准，
        # 返回空列表却是在说假话，后者严重得多。
        #
        # 退化的同时用 keyword_matched=False 明确告知调用方，
        # 让模型有机会说明「没找到名称含 xxx 的实验室」，
        # 而不是让用户以为系统里真的什么都没有。
        page = _lab_page(ctx, None)
        keyword_matched = False

    rows = [
        {
            "id": item.id,
            "name": item.name,
            "location": item.location,
            "capacity": item.capacity,
            "open_time": item.open_time,
            "close_time": item.close_time,
        }
        for item in page.list
    ]
    return {
        "ok": True,
        "total": page.total,
        "labs": rows,
        "keyword": keyword,
        "keyword_matched": keyword_matched,
    }


def _lab_page(ctx: AgentContext, keyword: str | None):
    """按关键词查开放实验室的一页（封装起来给上面的退化逻辑复用）。"""
    return lab_service.get_lab_page_list(
        ctx.db, page=1, page_size=20, keywords=keyword, status=1
    )


# ---------------------------------------------------------------------------
# 工具 3：实验室设备列表
# ---------------------------------------------------------------------------


@register
@tool(
    description=(
        "列出实验室里的设备（含 id、名称、型号、数量、状态）。"
        "要按设备预约时必须先调用它拿到设备 id。"
        "lab_name 可以不给：不给时会在**全部实验室**里按关键词搜，"
        "用来回答「哪个实验室有 X 设备」这类问题，"
        "此时每条结果都会带 lab_name 告诉你它在哪个实验室。"
    ),
    extras={"label": "查询实验室设备"},
)
def list_lab_equipments(
    ctx: Annotated[AgentContext, InjectedToolArg],
    lab_name: Annotated[
        str, Field(description="实验室名称；不给表示在全部实验室里搜")
    ] = "",
    keywords: Annotated[str, Field(description="设备名称关键词，可为空")] = "",
) -> dict:
    keyword = (keywords or "").strip() or None
    name = (lab_name or "").strip()

    # 没给实验室名 = 用户是按设备找实验室（「帮我约一个有 RTX5090 的实验室」）。
    # 这条分支是必需的：模型本来就会这样调 —— 实测它在被拒之前
    # 已经准确发出了 list_lab_equipments({"keywords": "RTX5090"})，
    # 只是撞上了 required 校验，然后被迫改调 list_open_labs，
    # 于是「哪间实验室有这张卡」变成了「先列出所有实验室」。
    if not name:
        return _search_equipments_across_labs(ctx, keyword)

    lab = _require_lab(ctx, name)
    page = _equipment_page(ctx, lab.id, keyword)
    keyword_matched = True
    if keyword and page.total == 0:
        # 同 list_open_labs：设备关键词零命中就退回全量，
        # 避免把「模型关键词写歪」演成「这个实验室没有设备」。
        page = _equipment_page(ctx, lab.id, None)
        keyword_matched = False

    rows = [
        {
            "id": item.id,
            "name": item.name,
            "spec": item.spec,
            "quantity": item.quantity,
            "status": item.status,
        }
        for item in page.list
    ]
    return {
        "ok": True,
        "lab_id": lab.id,
        "lab_name": lab.name,
        "equipments": rows,
        "keyword": keyword,
        "keyword_matched": keyword_matched,
    }


def _search_equipments_across_labs(ctx: AgentContext, keyword: str | None) -> dict:
    """跨全部实验室按设备名找设备。

    服务层的 get_equipment_page_list 本来就支持 lab_id=None（不过滤），
    所以这里不写新 SQL，只是换一种调用方式。

    与单实验室查询最重要的区别：**不做「零命中就退回全量」的退化**。
    这是两种性质不同的请求 ——
      「X 实验室有哪些设备」是在**列举**，全量退化最多是不够精准；
      「哪间实验室有 X 设备」是在**查存在性**，退回全量会把「没有这张卡」
      偷换成「系统里有 33 件设备」，反而把用户问的那件事弄丢了。

    但也不能直接用关键词查一次了事：模型填关键词时爱把问句里的词一起塞进来
    （实测同一个问题里它给 list_open_labs 填的是 keywords="RTX5090 实验室"）。
    所以零命中时按空白拆词，用最长的那几个词再各试一次 ——
    这样「RTX5090 实验室」能退到「RTX5090」命中，
    而真正不存在的设备则是所有词都查不到，可以诚实地回答「没有」。
    """
    page = _equipment_page(ctx, None, keyword)
    matched = bool(keyword)

    if keyword and page.total == 0:
        for token in _keyword_tokens(keyword):
            retry = _equipment_page(ctx, None, token)
            if retry.total:
                page, keyword, matched = retry, token, True
                break
        else:
            matched = False

    rows = [
        {
            "id": item.id,
            "name": item.name,
            "spec": item.spec,
            "quantity": item.quantity,
            "status": item.status,
            # 跨实验室搜索必须带出所属实验室，否则模型只拿到一个
            # 设备 id，还是不知道要约哪间实验室。
            "lab_id": item.lab_id,
            "lab_name": item.lab_name,
        }
        for item in page.list
    ]
    return {
        "ok": True,
        "scope": "all_labs",
        "total": page.total,
        "equipments": rows,
        "keyword": keyword,
        "keyword_matched": matched,
    }


def _keyword_tokens(keyword: str) -> list[str]:
    """把可能写歪的关键词拆成若干个可单独试的词，长的优先。

    只返回长度 >= 2 且与原串不等的词：单字太容易误命中，
    与原串相等则是原样重试，没有意义。
    """
    parts = {part for part in keyword.split() if len(part) >= 2}
    parts.discard(keyword)
    return sorted(parts, key=len, reverse=True)


def _equipment_page(ctx: AgentContext, lab_id: int | None, keyword: str | None):
    """查设备的一页（封装起来给上面的退化与跨实验室搜索复用）。"""
    return equipment_service.get_equipment_page_list(
        ctx.db, page=1, page_size=20, lab_id=lab_id, keywords=keyword
    )


# ---------------------------------------------------------------------------
# 工具 4：实验室可用性查询（核心）
# ---------------------------------------------------------------------------


@register
@tool(
    description=(
        "查询指定实验室在指定日期的开放窗口、已被占用的时段、以及剩余空闲时段。"
        "判断「能不能约」必须用它。"
    ),
    extras={"label": "查询实验室可用状态"},
)
def query_lab_availability(
    ctx: Annotated[AgentContext, InjectedToolArg],
    lab_name: Annotated[str, Field(description="实验室名称")],
    date: Annotated[str, Field(description="日期 YYYY-MM-DD")],
    start_time: Annotated[
        str | None, Field(description="期望开始时间 HH:MM，可为空")
    ] = None,
    end_time: Annotated[
        str | None, Field(description="期望结束时间 HH:MM，可为空")
    ] = None,
) -> dict:
    lab = _require_lab(ctx, lab_name)
    day = (date or "").strip() or datetime.now().strftime("%Y-%m-%d")
    availability = reservation_service.get_lab_availability(ctx.db, lab, day)

    payload: dict[str, Any] = {
        "ok": True,
        "lab_open": lab.status == 1,
        **availability,
    }

    # 用户给了明确时段时，直接给出「能用/不能用」的结论，
    # 让 Plan 后续步骤可以据此判断，而不用自己做区间重叠计算。
    if start_time and end_time:
        conflict = reservation_service.has_conflict(
            ctx.db, lab.id, day, start_time, end_time
        )
        payload["requested"] = {
            "start_time": start_time,
            "end_time": end_time,
            "available": not conflict,
        }
        if conflict:
            payload["requested"]["conflict_reason"] = "该时段已被其它预约占用"
        if lab.open_time and start_time < lab.open_time:
            payload["requested"]["available"] = False
            payload["requested"]["conflict_reason"] = "早于实验室开放时间"
        if lab.close_time and end_time > lab.close_time:
            payload["requested"]["available"] = False
            payload["requested"]["conflict_reason"] = "晚于实验室关闭时间"

    return payload


# ---------------------------------------------------------------------------
# 工具 5：用户权限 / 配额校验
# ---------------------------------------------------------------------------


@register
@tool(
    description=(
        "检查当前用户是否有权预约该实验室："
        "账号是否正常、实验室是否开放、预约配额是否用满。"
    ),
    extras={"label": "检查用户预约权限"},
)
def check_user_permission(
    ctx: Annotated[AgentContext, InjectedToolArg],
    lab_name: Annotated[str, Field(description="实验室名称")],
) -> dict:
    """⚠️ 注意：这里刻意**不接受 user_id 参数**。

    需求文档里写的是 check_user_permission(user_id, lab_id)，但让 LLM 传 user_id
    等于把越权能力交给模型 —— 它完全可以传别人的 id。
    用户身份只能来自 JWT 解析出的 ctx.user，这一点没有商量余地。
    """
    user = ctx.user
    reasons: list[str] = []

    if user.status != 1:
        reasons.append("账号已被禁用")

    lab = _find_lab(ctx, lab_name)
    if lab is None:
        reasons.append(f"实验室「{lab_name}」不存在")
    elif lab.status != 1:
        reasons.append(f"实验室「{lab.name}」当前未开放")

    active = reservation_service.count_active_reservations(ctx.db, user.id)
    if active >= MAX_ACTIVE_RESERVATIONS:
        reasons.append(
            f"已有 {active} 条进行中的预约，达到上限 {MAX_ACTIVE_RESERVATIONS} 条"
        )

    return {
        "ok": True,
        "allowed": not reasons,
        "user_name": user.name,
        "role": user.role,
        "lab_name": lab.name if lab else None,
        "lab_id": lab.id if lab else None,
        "active_reservations": active,
        "quota": MAX_ACTIVE_RESERVATIONS,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# 工具 6：寻找替代时段（失败补救）
# ---------------------------------------------------------------------------


@register
@tool(
    description=(
        "当用户想要的时间不可用时，在同一实验室同一日期内寻找其它满足时长的空闲时段。"
        "是「重新规划」的关键工具。"
    ),
    extras={"label": "查找替代空闲时段"},
)
def find_available_slots(
    ctx: Annotated[AgentContext, InjectedToolArg],
    lab_name: Annotated[str, Field(description="实验室名称")],
    date: Annotated[str, Field(description="日期 YYYY-MM-DD")],
    duration_hours: Annotated[
        float | None, Field(description="需要的时长（小时）")
    ] = None,
    preferred_start: Annotated[
        str | None,
        Field(description="用户原本期望的开始时间 HH:MM，用于优先推荐最接近的时段"),
    ] = None,
) -> dict:
    lab = _require_lab(ctx, lab_name)
    day = (date or "").strip() or datetime.now().strftime("%Y-%m-%d")
    hours = float(duration_hours) if duration_hours else 3.0
    hours = min(max(hours, 1.0), 8.0)  # 夹到 1~8 小时，防止模型给个 0.1 或 100

    slots = reservation_service.find_alternative_slots(
        ctx.db,
        lab,
        day,
        duration_minutes=int(hours * 60),
        preferred_start=preferred_start,
        limit=3,
    )
    return {
        "ok": True,
        "lab_id": lab.id,
        "lab_name": lab.name,
        "date": day,
        "duration_hours": hours,
        "slots": slots,
        "has_slot": bool(slots),
    }


# ---------------------------------------------------------------------------
# 工具 7：创建预约（唯一有副作用的工具）
# ---------------------------------------------------------------------------


@register
@tool(
    description=(
        "真实创建一条预约记录（状态为待审核）。" "只有在用户明确确认后才可以调用。"
    ),
    extras={"label": "提交预约"},
)
def create_reservation(
    ctx: Annotated[AgentContext, InjectedToolArg],
    lab_name: Annotated[str, Field(description="实验室名称")],
    date: Annotated[str, Field(description="日期 YYYY-MM-DD")],
    start_time: Annotated[str, Field(description="开始时间 HH:MM")],
    end_time: Annotated[str, Field(description="结束时间 HH:MM")],
    equipment_name: Annotated[
        str | None, Field(description="设备名称，只约实验室时留空")
    ] = None,
    remark: Annotated[str | None, Field(description="备注，可为空")] = None,
) -> dict:
    lab = _require_lab(ctx, lab_name)

    equipment_id = None
    if equipment_name:
        equipment = _find_equipment(ctx, lab.id, equipment_name)
        if equipment is None:
            return {
                "ok": False,
                "error": f"实验室「{lab.name}」下没有名为「{equipment_name}」的设备",
            }
        equipment_id = equipment.id

    # 复用 service 层，所有业务校验（日期、开放时间、时段冲突、设备状态）
    # 都在那里做，工具层不重复实现，避免两套规则打架。
    payload = ReservationCreateRequest(
        lab_id=lab.id,
        equipment_id=equipment_id,
        date=date,
        start_time=start_time,
        end_time=end_time,
        remark=remark or None,
    )
    reservation_service.create_reservation(ctx.db, ctx.user, payload)

    latest = reservation_service.get_latest_reservation(
        ctx.db, ctx.user.id, lab_id=lab.id, date=date
    )
    return {
        "ok": True,
        "reservation_id": latest.id if latest else None,
        "lab_id": lab.id,
        "lab_name": lab.name,
        "equipment_name": equipment_name,
        "date": date,
        "start_time": start_time,
        "end_time": end_time,
        "status": "待审核",
        "message": "预约已提交，需管理员审核通过后方可使用",
    }


# ---------------------------------------------------------------------------
# 工具 8：预约结果核验
# ---------------------------------------------------------------------------


@register
@tool(
    description=(
        "回查数据库，核验预约是否真的落库、当前状态是什么。" "由「验证」步骤调用。"
    ),
    extras={"label": "验证预约结果"},
)
def verify_reservation(
    ctx: Annotated[AgentContext, InjectedToolArg],
    lab_name: Annotated[str, Field(description="实验室名称")],
    date: Annotated[str, Field(description="日期 YYYY-MM-DD")] = "",
) -> dict:
    lab = _find_lab(ctx, lab_name)
    item = reservation_service.get_latest_reservation(
        ctx.db,
        ctx.user.id,
        lab_id=lab.id if lab else None,
        date=(date or "").strip() or None,
    )
    if item is None:
        return {
            "ok": True,
            "exists": False,
            "message": "数据库中没有找到对应预约记录",
        }
    return {
        "ok": True,
        "exists": True,
        "reservation_id": item.id,
        "lab_name": item.lab.name if item.lab else None,
        "equipment_name": item.equipment.name if item.equipment else None,
        "date": item.date,
        "start_time": item.start_time,
        "end_time": item.end_time,
        "status": item.status,
        "status_text": RESERVATION_STATUS_TEXT.get(item.status, "未知"),
        "verified": True,
    }


# ---------------------------------------------------------------------------
# 工具 9：查询我的预约
# ---------------------------------------------------------------------------


@register
@tool(
    description=(
        "查询当前登录用户自己的预约记录（返回最近若干条，含预约编号和状态）。"
        "用户问「我最近约了什么」「我的预约还在吗」「查一下我的预约记录」时用它；"
        "用户要取消预约但没说编号时，也先用它拿到编号。"
    ),
    extras={"label": "查询我的预约"},
)
def query_user_reservations(
    ctx: Annotated[AgentContext, InjectedToolArg],
    status: Annotated[
        Any,
        Field(description="按状态筛选：0待审核 1已通过 2已拒绝 3已取消；不筛就不传"),
    ] = None,
    limit: Annotated[Any, Field(description="最多返回几条，默认 5")] = None,
) -> dict:
    """查询当前用户的预约记录。

    ⚠️ 和 check_user_permission 一样，这里刻意**不接受 user_id 参数**。
    用户只能看到自己的预约 —— 身份来自 JWT 解析出的 ctx.user，
    让模型传 user_id 等于把「翻别人的预约记录」这个能力交给它。
    service 层的 get_reservation_page_list 内部还按角色又过滤了一道
    （非管理员强制加 user_id 条件），这是双保险。
    """
    size = _as_int(limit) or 5
    # 夹到 1~20：模型偶尔会要 10000 条，那会把一次工具返回变成几十 KB，
    # 既撑爆上下文，也让 Trace Panel 没法读。
    size = min(max(size, 1), 20)

    status_filter = _as_int(status)
    if status_filter not in RESERVATION_STATUS_TEXT:
        # 模型可能编一个不存在的状态（比如 9），当成“不过滤”比报错好：
        # 用户的本意是“看看我的预约”，不该因为模型多填了个数字就什么都看不到。
        status_filter = None

    page = reservation_service.get_reservation_page_list(
        ctx.db, ctx.user, page=1, page_size=size, status=status_filter
    )
    items = [_reservation_brief(row) for row in page.list]

    return {
        "ok": True,
        "user_name": ctx.user.name,
        "total": page.total,
        "returned": len(items),
        "reservations": items,
    }


# ---------------------------------------------------------------------------
# 工具 10：取消预约（写库）
# ---------------------------------------------------------------------------


@register
@tool(
    description=(
        "取消当前用户自己的一条预约。这是**写库操作**，只在用户明确要求取消时调用。"
        "优先传 reservation_id；用户没给编号时，先用 query_user_reservations 查到编号。"
        "只有「待审核」状态的预约可以取消。"
    ),
    extras={"label": "取消预约"},
)
def cancel_reservation(
    ctx: Annotated[AgentContext, InjectedToolArg],
    reservation_id: Annotated[
        Any,
        Field(description="预约编号，优先使用；应来自 query_user_reservations 的返回"),
    ] = None,
    lab_name: Annotated[
        str | None, Field(description="实验室名称，没有编号时用它定位要取消哪一条")
    ] = None,
    date: Annotated[
        str | None, Field(description="预约日期 YYYY-MM-DD，配合实验室名称定位")
    ] = None,
) -> dict:
    """取消预约。

    ⚠️ 四个设计要点：

    1. **身份只来自 ctx.user**，不接受模型传 user_id。
    2. **每一条查询都先按 user_id 过滤**，再做后续匹配。这样即使模型给了
       别人的 reservation_id，结果也只是「你自己的记录里没有这一条」，
       而不是去动别人的单。service 层还有一道 user_id 校验兜底，
       但让「查不到」先发生，错误信息才对用户有意义。
    3. **匹配到多条时绝不替用户挑一条删掉**。删除是不可逆的，
       宁可选不了让用户指明，也不能猜 —— 猜错的代价是删掉一条用户还要用的预约。
    4. 只能取消「待审核」的预约，这条业务规则由 service 层给出，
       工具层只做**提前判断 + 好文案**，不重复实现校验。
    """
    target: Reservation | None = None
    wanted_id = _as_int(reservation_id)

    if wanted_id is not None:
        # 按 id 定位：user_id 必须一起进 WHERE，越权在这里就被挡住了
        target = (
            ctx.db.query(Reservation)
            .filter(
                Reservation.id == wanted_id,
                Reservation.user_id == ctx.user.id,
            )
            .first()
        )
        if target is None:
            return {
                "ok": False,
                "error": f"你的预约记录里没有编号为 {wanted_id} 的预约",
            }
    else:
        # 没给编号：在「自己的预约」里按实验室/日期找
        query = ctx.db.query(Reservation).filter(Reservation.user_id == ctx.user.id)
        if date and str(date).strip():
            query = query.filter(Reservation.date == str(date).strip())
        if lab_name and str(lab_name).strip():
            lab = _find_lab(ctx, str(lab_name))
            if lab is None:
                return {
                    "ok": False,
                    "error": f"没有找到名为「{lab_name}」的实验室",
                }
            query = query.filter(Reservation.lab_id == lab.id)

        candidates = query.order_by(Reservation.id.desc()).limit(5).all()
        if not candidates:
            return {
                "ok": False,
                "error": (
                    "你的预约记录里没有符合条件的预约"
                    + (f"（实验室：{lab_name}）" if lab_name else "")
                    + (f"（日期：{date}）" if date else "")
                ),
            }

        # 待审核的优先：只有它能被取消，直接挑一条不能取消的只会白跑一趟。
        # 显式注解是必要的：SQLAlchemy 的 Query.filter() 推断会退化成 Any，
        # 一旦 cancelable 跟着变成 Any，下面 target 的 None 收窄就失效了。
        cancelable: list[Reservation] = [
            row for row in candidates if row.status == CANCELABLE_STATUS
        ]
        if not cancelable:
            return {
                "ok": False,
                "error": (
                    "符合条件的预约当前状态不支持取消"
                    "（只有「待审核」的预约可以取消），可能已通过审核、已被拒绝或已取消"
                ),
                "candidates": [_reservation_brief(row) for row in candidates],
            }
        if len(cancelable) > 1:
            # 不可逆操作 + 目标不唯一 → 交给上层追问，绝不猜
            return {
                "ok": False,
                "error": (
                    f"匹配到 {len(cancelable)} 条待审核的预约，无法确定要取消哪一条，"
                    "需要用户指明预约编号"
                ),
                "candidates": [_reservation_brief(row) for row in cancelable],
            }
        target = cancelable[0]

    # 复用 service：状态校验、越权校验、改状态、提交事务都在那里
    reservation_service.cancel_reservation(ctx.db, ctx.user, target.id)

    return {
        "ok": True,
        **_reservation_brief(target),
        "status": 3,
        "status_text": "已取消",
        "message": "预约已取消，该时段已释放",
    }


# ---------------------------------------------------------------------------
# 工具 11：日期换算
# ---------------------------------------------------------------------------


@register
@tool(
    description=("获取服务器当前日期，以及相对日期（明天/后天）对应的日期字符串。"),
    extras={"label": "获取当前日期"},
)
def get_today(
    _: Annotated[AgentContext, InjectedToolArg],  # 注入的上下文，本工具用不到
    offset_days: Annotated[
        float | None, Field(description="相对今天偏移的天数，今天填 0，明天填 1")
    ] = None,
) -> dict:
    target = datetime.now() + timedelta(days=int(offset_days or 0))
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][target.weekday()]
    return {
        "ok": True,
        "date": target.strftime("%Y-%m-%d"),
        "weekday": weekday,
        "today": datetime.now().strftime("%Y-%m-%d"),
    }
