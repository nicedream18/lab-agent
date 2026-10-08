import asyncio
from datetime import datetime

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessException
from app.common.response import PageResponse
from app.database import SessionLocal
from app.models.equipment import Equipment
from app.models.lab import Lab
from app.models.reservation import Reservation
from app.models.user import User
from app.schemas.reservation import ReservationCreateRequest, ReservationResponse


def create_reservation(db: Session, current_user: User, data: ReservationCreateRequest):
    """创建预约记录"""
    now = datetime.now()
    current_date = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")
    if data.date < current_date:
        raise BusinessException(message="预约日期不能小于当前的日期")
    if data.end_time < data.start_time:
        raise BusinessException(message="预约的结束时间不能小于开始时间")
    if data.date == current_date and data.start_time < current_time:
        raise BusinessException(message="预约开始时间不能小于当前的时间")
    lab = db.query(Lab).filter(Lab.id == data.lab_id).first()
    if not lab:
        raise BusinessException(message="实验室不存在")
    if lab.status != 1:
        raise BusinessException(message="实验室已关闭")
    if lab.open_time and data.start_time < lab.open_time:
        raise BusinessException(message="预约时间不能早于实验室的开放时间")
    if lab.close_time and data.end_time > lab.close_time:
        raise BusinessException(message="预约时间不能晚于实验室的关闭时间")

    # equipment_id 不为空 则表示这次是预约的实验室设备
    if data.equipment_id:
        equipment = (
            db.query(Equipment).filter(Equipment.id == data.equipment_id).first()
        )
        if not equipment:
            raise BusinessException(message="实验室设备不存在")
        if equipment.status != 1:
            raise BusinessException(message="实验室设备正在维修")

    # 实验室或者设备是否是可预约的状态
    query = db.query(Reservation).filter(
        Reservation.lab_id == data.lab_id,
        Reservation.date == data.date,
        Reservation.status.in_([0, 1]),  # 0表示待审核  1已通过
        Reservation.start_time < data.end_time,
        Reservation.end_time > data.start_time,
    )
    if data.equipment_id:
        query = query.filter(
            Reservation.equipment_id == data.equipment_id
        )  # 预约实验室设备
    else:
        query = query.filter(Reservation.equipment_id.is_(None))  # 只预约实验室
    res = query.first()
    if res:
        raise BusinessException(message="该时段已预约")

    resvervation_model = Reservation(
        user_id=current_user.id,
        lab_id=data.lab_id,
        equipment_id=data.equipment_id,
        date=data.date,
        start_time=data.start_time,
        end_time=data.end_time,
        remark=data.remark,
        status=0,
    )
    db.add(resvervation_model)
    db.commit()


def get_reservation_page_list(
    db: Session,
    current_user: User,
    page: int,
    page_size: int,
    status: int | None = None,
):
    """查询预约的记录"""
    query = db.query(Reservation)
    if current_user.role != "admin":
        query = query.filter(Reservation.user_id == current_user.id)
    if status is not None:
        query = query.filter(Reservation.status == status)
    total = query.count()
    items = (
        query.order_by(Reservation.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    result = []
    for item in items:
        res = ReservationResponse.model_validate(item)
        res.user_name = item.user.name if item.user else None
        res.lab_name = item.lab.name if item.lab else None
        res.equipment_name = item.equipment.name if item.equipment else None
        res.type = "设备" if item.equipment_id else "实验室"
        result.append(res)

    return PageResponse(list=result, total=total)


def cancel_reservation(db: Session, current_user: User, reservation_id: int):
    """学生取消预约"""
    item = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not item:
        raise BusinessException(message="预约记录不存在")
    if item.user_id != current_user.id:
        raise BusinessException(message="无权限", code=403)
    if item.status != 0:
        raise BusinessException(message="当前状态无法取消")
    item.status = 3
    db.commit()


def audit_reservation(db: Session, reservation_id: int, status: int):
    """管理员审核预约"""
    if status not in [1, 2]:
        raise BusinessException(message="审核状态错误")
    item = db.query(Reservation).filter(Reservation.id == reservation_id).first()
    if not item:
        raise BusinessException(message="预约记录不存在")
    if item.status != 0:
        raise BusinessException(message="当前状态不支持审核")
    item.status = status
    db.commit()


def expire_pending_reservations():
    db = SessionLocal()
    try:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        now_time = now.strftime("%H:%M")
        items = db.query(Reservation).filter(Reservation.status == 0).all()
        changed = False
        for item in items:
            if item.date < today or (item.date == today and item.end_time <= now_time):
                item.status = 3
                changed = True
        if changed:
            db.commit()
    finally:
        db.close()


async def run_expire_scan():
    while True:
        expire_pending_reservations()
        await asyncio.sleep(60)


# ============================================================================
# 可用性查询（供 Agent 工具调用）
#
# 放在 service 层而不是 tools.py 里，是因为这属于「业务规则」：
# 什么算占用、粒度多粗、开放时间怎么取，都是实验室预约领域的定义。
# Tool 只应该是一层薄薄的适配器。
# ============================================================================

# 时间槽粒度：以 1 小时为最小可约单位
SLOT_MINUTES = 60

DEFAULT_OPEN_TIME = "08:00"
DEFAULT_CLOSE_TIME = "21:00"


def _to_minutes(value: str) -> int:
    """'9:30' / '09:30' → 570。

    数据库里 open_time 存的是 '08:00'，但模型抽出来的可能是 '9:00'，
    所以必须容错，不能直接 int(value[:2])。
    """
    text = (value or "").strip()
    parts = text.split(":")
    if len(parts) < 2:
        raise BusinessException(message=f"时间格式不正确：{value}")
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        raise BusinessException(message=f"时间格式不正确：{value}") from None


def _to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """两个时间区间是否重叠。[start, end) 左闭右开。

    条件写成 `a_start < b_end and a_end > b_start`，
    用 <= / >= 会把「上一场 12:00 结束、下一场 12:00 开始」误判成冲突。
    """
    return _to_minutes(start_a) < _to_minutes(end_b) and _to_minutes(
        end_a
    ) > _to_minutes(start_b)


def list_busy_slots(
    db: Session, lab_id: int, date: str, equipment_id: int | None = None
) -> list[dict]:
    """列出某实验室（或某台设备）在某天已被占用的时段。

    只统计 0(待审核) 和 1(已通过)，2(已拒绝)/3(已取消) 不算占用。
    """
    query = db.query(Reservation).filter(
        Reservation.lab_id == lab_id,
        Reservation.date == date,
        Reservation.status.in_([0, 1]),
    )
    if equipment_id:
        query = query.filter(Reservation.equipment_id == equipment_id)
    else:
        # 不指定设备时看的是「实验室整体」，此时只看不绑设备的预约记录
        query = query.filter(Reservation.equipment_id.is_(None))

    return [
        {"start_time": item.start_time, "end_time": item.end_time, "status": item.status}
        for item in query.order_by(Reservation.start_time).all()
    ]


def has_conflict(
    db: Session,
    lab_id: int,
    date: str,
    start_time: str,
    end_time: str,
    equipment_id: int | None = None,
) -> bool:
    """指定时段是否已被占用。"""
    for busy in list_busy_slots(db, lab_id, date, equipment_id):
        if _overlap(start_time, end_time, busy["start_time"], busy["end_time"]):
            return True
    return False


def get_lab_availability(db: Session, lab: Lab, date: str) -> dict:
    """给出某实验室某天的完整可用性视图：开放窗口 + 占用时段 + 空闲时段。"""
    open_time = lab.open_time or DEFAULT_OPEN_TIME
    close_time = lab.close_time or DEFAULT_CLOSE_TIME
    busy = list_busy_slots(db, lab.id, date)

    free = []
    cursor = _to_minutes(open_time)
    close_minutes = _to_minutes(close_time)
    while cursor + SLOT_MINUTES <= close_minutes:
        slot_start = _to_hhmm(cursor)
        slot_end = _to_hhmm(cursor + SLOT_MINUTES)
        if not any(
            _overlap(slot_start, slot_end, item["start_time"], item["end_time"])
            for item in busy
        ):
            free.append({"start_time": slot_start, "end_time": slot_end})
        cursor += SLOT_MINUTES

    return {
        "lab_id": lab.id,
        "lab_name": lab.name,
        "location": lab.location,
        "date": date,
        "open_time": open_time,
        "close_time": close_time,
        "open_status": lab.status,
        "busy_slots": busy,
        "free_slots": free,
    }


def find_alternative_slots(
    db: Session,
    lab: Lab,
    date: str,
    duration_minutes: int = 180,
    preferred_start: str | None = None,
    limit: int = 3,
) -> list[dict]:
    """在给定日期内寻找满足时长的空闲连续时段。

    排序策略：优先离用户原本想要的时间最近的时段。
    这比「从早到晚顺排」更贴近真实推荐逻辑 —— 用户说下午 2 点，
    推荐 15:00 远比推荐 08:00 有用。
    """
    avail = get_lab_availability(db, lab, date)
    free = avail["free_slots"]
    if not free:
        return []

    # 把连续的空闲小时槽合并成区间，否则 14-15 / 15-16 / 16-17 会被当成三段
    merged: list[dict] = []
    for slot in free:
        if merged and merged[-1]["end_time"] == slot["start_time"]:
            merged[-1]["end_time"] = slot["end_time"]
        else:
            merged.append(dict(slot))

    target = _to_minutes(preferred_start) if preferred_start else None
    candidates = []
    for block in merged:
        start_m = _to_minutes(block["start_time"])
        end_m = _to_minutes(block["end_time"])
        cursor = start_m
        while cursor + duration_minutes <= end_m:
            candidates.append(
                {
                    "start_time": _to_hhmm(cursor),
                    "end_time": _to_hhmm(cursor + duration_minutes),
                    "gap": abs(cursor - target) if target is not None else 0,
                }
            )
            cursor += SLOT_MINUTES

    candidates.sort(key=lambda item: (item["gap"], item["start_time"]))
    for item in candidates:
        item.pop("gap", None)
    return candidates[:limit]


def get_latest_reservation(
    db: Session,
    user_id: int,
    lab_id: int | None = None,
    date: str | None = None,
) -> Reservation | None:
    """查用户最近一条预约，用于预约后的结果核验。"""
    query = db.query(Reservation).filter(Reservation.user_id == user_id)
    if lab_id:
        query = query.filter(Reservation.lab_id == lab_id)
    if date:
        query = query.filter(Reservation.date == date)
    return query.order_by(Reservation.id.desc()).first()


def count_active_reservations(db: Session, user_id: int) -> int:
    """用户名下「待审核 + 已通过」的预约总数，用于配额校验。"""
    return (
        db.query(Reservation)
        .filter(Reservation.user_id == user_id, Reservation.status.in_([0, 1]))
        .count()
    )
