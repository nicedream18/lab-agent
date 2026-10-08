"""Agent 的工具层。

设计原则：**LLM 永远不直接碰数据库**。
所有业务能力都封装成这里注册的函数，LLM 只能「申请调用哪个工具、传什么参数」，
真正的数据库读写全部发生在受控的 Python 代码里。

这样做的三个好处：
1. 安全 —— LLM 无法构造任意 SQL，也无法伪造 user_id 越权操作别人的预约；
2. 可控 —— 参数在进入业务逻辑前会先过白名单和类型校验；
3. 可测 —— 每个工具都能脱离 LLM 单独做单元测试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from app.agent.state import AgentContext
from app.common.exceptions import BusinessException
from app.models.equipment import Equipment
from app.models.lab import Lab
from app.schemas.reservation import ReservationCreateRequest
from app.services import (
    equipment_service,
    kb_service,
    lab_service,
    reservation_service,
)

# 单个工具返回给模型的最大字符数。不做限制的话，
# 一次实验室列表查询就能把上下文撑爆，且浪费 token。
MAX_RESULT_CHARS = 1500

# 用户名下「待审核 + 已通过」预约的配额上限
MAX_ACTIVE_RESERVATIONS = 5


@dataclass(frozen=True)
class AgentTool:
    name: str
    label: str  # 前端展示用的中文名
    description: str
    parameters: dict[str, Any]  # JSON Schema 风格的参数定义
    handler: Callable[..., dict]


TOOL_REGISTRY: dict[str, AgentTool] = {}


def agent_tool(
    name: str, label: str, description: str, parameters: dict[str, Any]
) -> Callable:
    """把一个普通函数注册成 Agent 工具。"""

    def decorator(func: Callable) -> Callable:
        TOOL_REGISTRY[name] = AgentTool(
            name=name,
            label=label,
            description=description,
            parameters=parameters,
            handler=func,
        )
        return func

    return decorator


def tool_catalog() -> str:
    """把工具清单渲染成文本，喂给 Planner 提示词。"""
    lines = []
    for tool in TOOL_REGISTRY.values():
        params = ", ".join(
            f"{key}:{value.get('type', 'string')}"
            for key, value in tool.parameters.get("properties", {}).items()
        )
        lines.append(f"- {tool.name}({params}) — {tool.description}")
    return "\n".join(lines)


def tool_parameters(name: str) -> str:
    tool = TOOL_REGISTRY.get(name)
    if not tool:
        return "（未知工具）"
    return json.dumps(tool.parameters, ensure_ascii=False, indent=2)


def tool_label(name: str) -> str:
    tool = TOOL_REGISTRY.get(name)
    return tool.label if tool else name


def run_tool(ctx: AgentContext, name: str, args: dict[str, Any]) -> dict:
    """执行工具，把异常统一收敛成 {"ok": False, "error": ...}。

    这里**必须**吞掉异常而不是往外抛：工具失败对 Agent 来说是
    「一个可反思的观察结果」，而不是「整个流程崩掉」。
    """
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        return {"ok": False, "error": f"工具 {name} 不存在"}

    # 过掉 args 里多余的键：模型经常多塞几个没定义的参数，
    # 直接 **args 会 TypeError，所以要按参数定义做白名单过滤。
    # 顺手丢掉 None / "" / [] 的占位参数：模型爱把可选参数写成 null，
    # 留着既污染 trace 里的调用记录，又会盖掉函数本身的默认值。
    allowed = set(tool.parameters.get("properties", {}).keys())
    clean = {
        key: value
        for key, value in (args or {}).items()
        if key in allowed and value not in (None, "", [])
    }

    try:
        payload = tool.handler(ctx, **clean)
        payload.setdefault("ok", True)
        return payload
    except BusinessException as exc:
        return {"ok": False, "error": exc.message}
    except TypeError as exc:
        # 参数缺失/类型不对，是模型规划的锅，属于可反思错误
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
        ctx.db.query(Lab)
        .filter(Lab.name.ilike(f"%{name}%"))
        .order_by(Lab.id)
        .first()
    )


def _find_equipment(ctx: AgentContext, lab_id: int, equipment_name: str) -> Equipment | None:
    if not equipment_name or not lab_id:
        return None
    name = equipment_name.strip()
    query = ctx.db.query(Equipment).filter(Equipment.lab_id == lab_id)
    return query.filter(Equipment.name == name).first() or query.filter(
        Equipment.name.ilike(f"%{name}%")
    ).first()


def _require_lab(ctx: AgentContext, lab_name: str) -> Lab:
    # 空名称要单独报错。不然后面会拼出「没有找到名为「None」的实验室」,
    # 把“参数丢了”伪装成“实验室不存在”，排查时非常浪费时间。
    if not (lab_name or "").strip():
        raise BusinessException(message="缺少实验室名称，无法执行该操作")
    lab = _find_lab(ctx, lab_name)
    if lab is None:
        raise BusinessException(message=f"没有找到名为「{lab_name}」的实验室")
    return lab


# ---------------------------------------------------------------------------
# 工具 1：知识库检索
# ---------------------------------------------------------------------------


@agent_tool(
    name="search_lab_docs",
    label="检索实验室知识库",
    description="查询实验室的规章制度、安全规范、开放时间等制度性资料。问「规则」「能不能带食物」「安全要求」时用它。",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "检索关键词"}},
        "required": ["query"],
    },
)
def search_lab_docs(ctx: AgentContext, query: str = "") -> dict:
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


@agent_tool(
    name="list_open_labs",
    label="查询开放实验室",
    description="列出所有开放中的实验室（含 id、名称、位置、容量、开放时间）。用户没给准确实验室名时先用它确认。",
    parameters={
        "type": "object",
        "properties": {
            "keywords": {"type": "string", "description": "名称关键词，可为空"}
        },
    },
)
def list_open_labs(ctx: AgentContext, keywords: str = "") -> dict:
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


@agent_tool(
    name="list_lab_equipments",
    label="查询实验室设备",
    description="列出某个实验室里的设备（含 id、名称、型号、数量、状态）。要按设备预约时必须先调用它拿到设备 id。",
    parameters={
        "type": "object",
        "properties": {
            "lab_name": {"type": "string", "description": "实验室名称"},
            "keywords": {"type": "string", "description": "设备名称关键词，可为空"},
        },
        "required": ["lab_name"],
    },
)
def list_lab_equipments(ctx: AgentContext, lab_name: str = "", keywords: str = "") -> dict:
    lab = _require_lab(ctx, lab_name)
    keyword = (keywords or "").strip() or None
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


def _equipment_page(ctx: AgentContext, lab_id: int, keyword: str | None):
    """查某实验室设备的一页（封装起来给上面的退化逻辑复用）。"""
    return equipment_service.get_equipment_page_list(
        ctx.db, page=1, page_size=20, lab_id=lab_id, keywords=keyword
    )


# ---------------------------------------------------------------------------
# 工具 4：实验室可用性查询（核心）
# ---------------------------------------------------------------------------


@agent_tool(
    name="query_lab_availability",
    label="查询实验室可用状态",
    description="查询指定实验室在指定日期的开放窗口、已被占用的时段、以及剩余空闲时段。判断「能不能约」必须用它。",
    parameters={
        "type": "object",
        "properties": {
            "lab_name": {"type": "string", "description": "实验室名称"},
            "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
            "start_time": {"type": "string", "description": "期望开始时间 HH:MM，可为空"},
            "end_time": {"type": "string", "description": "期望结束时间 HH:MM，可为空"},
        },
        "required": ["lab_name", "date"],
    },
)
def query_lab_availability(
    ctx: AgentContext,
    lab_name: str = "",
    date: str = "",
    start_time: str | None = None,
    end_time: str | None = None,
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


@agent_tool(
    name="check_user_permission",
    label="检查用户预约权限",
    description="检查当前用户是否有权预约该实验室：账号是否正常、实验室是否开放、预约配额是否用满。",
    parameters={
        "type": "object",
        "properties": {"lab_name": {"type": "string", "description": "实验室名称"}},
        "required": ["lab_name"],
    },
)
def check_user_permission(ctx: AgentContext, lab_name: str = "") -> dict:
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


@agent_tool(
    name="find_available_slots",
    label="查找替代空闲时段",
    description="当用户想要的时间不可用时，在同一实验室同一日期内寻找其它满足时长的空闲时段。是「重新规划」的关键工具。",
    parameters={
        "type": "object",
        "properties": {
            "lab_name": {"type": "string", "description": "实验室名称"},
            "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
            "duration_hours": {"type": "number", "description": "需要的时长（小时）"},
            "preferred_start": {
                "type": "string",
                "description": "用户原本期望的开始时间 HH:MM，用于优先推荐最接近的时段",
            },
        },
        "required": ["lab_name", "date"],
    },
)
def find_available_slots(
    ctx: AgentContext,
    lab_name: str = "",
    date: str = "",
    duration_hours: float | None = None,
    preferred_start: str | None = None,
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


@agent_tool(
    name="create_reservation",
    label="提交预约",
    description="真实创建一条预约记录（状态为待审核）。只有在用户明确确认后才可以调用。",
    parameters={
        "type": "object",
        "properties": {
            "lab_name": {"type": "string", "description": "实验室名称"},
            "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
            "start_time": {"type": "string", "description": "开始时间 HH:MM"},
            "end_time": {"type": "string", "description": "结束时间 HH:MM"},
            "equipment_name": {
                "type": "string",
                "description": "设备名称，只约实验室时留空",
            },
            "remark": {"type": "string", "description": "备注，可为空"},
        },
        "required": ["lab_name", "date", "start_time", "end_time"],
    },
)
def create_reservation(
    ctx: AgentContext,
    lab_name: str = "",
    date: str = "",
    start_time: str = "",
    end_time: str = "",
    equipment_name: str | None = None,
    remark: str | None = None,
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


@agent_tool(
    name="verify_reservation",
    label="验证预约结果",
    description="回查数据库，核验预约是否真的落库、当前状态是什么。由「验证」步骤调用。",
    parameters={
        "type": "object",
        "properties": {
            "lab_name": {"type": "string", "description": "实验室名称"},
            "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
        },
        "required": ["lab_name"],
    },
)
def verify_reservation(ctx: AgentContext, lab_name: str = "", date: str = "") -> dict:
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
    status_text = {0: "待审核", 1: "已通过", 2: "已拒绝", 3: "已取消"}.get(
        item.status, "未知"
    )
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
        "status_text": status_text,
        "verified": True,
    }


# ---------------------------------------------------------------------------
# 工具 9：日期换算
# ---------------------------------------------------------------------------


@agent_tool(
    name="get_today",
    label="获取当前日期",
    description="获取服务器当前日期，以及相对日期（明天/后天）对应的日期字符串。",
    parameters={
        "type": "object",
        "properties": {
            "offset_days": {
                "type": "number",
                "description": "相对今天偏移的天数，今天填 0，明天填 1",
            }
        },
    },
)
def get_today(ctx: AgentContext, offset_days: float | None = None) -> dict:
    target = datetime.now() + timedelta(days=int(offset_days or 0))
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][
        target.weekday()
    ]
    return {
        "ok": True,
        "date": target.strftime("%Y-%m-%d"),
        "weekday": weekday,
        "today": datetime.now().strftime("%Y-%m-%d"),
    }
