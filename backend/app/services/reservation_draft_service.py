"""预约草稿的持久化、确认和跨请求去重。"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.equipment import Equipment
from app.models.lab import Lab
from app.models.reservation import Reservation
from app.models.reservation_draft import ReservationDraft

TTL = timedelta(minutes=20)
FIELDS = ("lab_name", "equipment_name", "date", "start_time", "end_time")
REQUIRED = ("lab_name", "date", "start_time", "end_time")


def target_from(values: dict | None) -> dict:
    return {key: str(values[key]).strip() for key in FIELDS if (values or {}).get(key)}


def complete(target: dict) -> bool:
    return all(target.get(key) for key in REQUIRED)


def load(db: Session, user_id: int, conversation_id: str) -> ReservationDraft | None:
    if not conversation_id:
        return None
    draft = (
        db.query(ReservationDraft)
        .filter_by(user_id=user_id, conversation_id=conversation_id)
        .first()
    )
    if draft is None or draft.expires_at <= datetime.now():
        return None
    return draft


def save(
    db: Session, user_id: int, conversation_id: str, target: dict, *, requested: bool
) -> ReservationDraft:
    target = target_from(target)
    if target.get("lab_name"):
        name = target["lab_name"]
        lab = (
            db.query(Lab).filter(Lab.name == name).first()
            or db.query(Lab)
            .filter(Lab.name.ilike(f"%{name}%"))
            .order_by(Lab.id)
            .first()
        )
        if lab:
            target["lab_name"] = lab.name
            if target.get("equipment_name"):
                name = target["equipment_name"]
                equipment = (
                    db.query(Equipment)
                    .filter(Equipment.lab_id == lab.id, Equipment.name == name)
                    .first()
                    or db.query(Equipment)
                    .filter(
                        Equipment.lab_id == lab.id, Equipment.name.ilike(f"%{name}%")
                    )
                    .first()
                )
                if equipment:
                    target["equipment_name"] = equipment.name
    draft = (
        db.query(ReservationDraft)
        .filter_by(user_id=user_id, conversation_id=conversation_id)
        .first()
    )
    if draft is None:
        draft = ReservationDraft(
            user_id=user_id, conversation_id=conversation_id, revision=0
        )
        db.add(draft)
    if draft.status == "submitting":
        return draft
    draft.target = target
    draft.requested = requested
    draft.status = "ready"
    draft.reservation_id = None
    draft.prior_reservation_id = None
    draft.revision = (draft.revision or 0) + 1
    draft.expires_at = datetime.now() + TTL
    db.commit()
    return draft


def matches(target: dict, args: dict) -> bool:
    return all(
        str(args.get(key) or "").strip() == str(target.get(key) or "").strip()
        for key in FIELDS
    )


def find_existing(db: Session, draft: ReservationDraft) -> Reservation | None:
    """进程在写入预约后、标记草稿前退出时，按用户和目标核对是否已提交。"""
    target = draft.target or {}
    if not complete(target):
        return None
    lab = db.query(Lab).filter_by(name=target["lab_name"]).first()
    if lab is None:
        return None
    query = (
        db.query(Reservation)
        .filter_by(
            user_id=draft.user_id,
            lab_id=lab.id,
            date=target["date"],
            start_time=target["start_time"],
            end_time=target["end_time"],
        )
        .filter(Reservation.status.in_([0, 1]))
    )
    if target.get("equipment_name"):
        equipment = (
            db.query(Equipment)
            .filter_by(lab_id=lab.id, name=target["equipment_name"])
            .first()
        )
        if equipment is None:
            return None
        query = query.filter_by(equipment_id=equipment.id)
    else:
        query = query.filter(Reservation.equipment_id.is_(None))
    return query.order_by(Reservation.id.desc()).first()


def claim(db: Session, draft: ReservationDraft, args: dict) -> tuple[bool, str]:
    if not matches(draft.target or {}, args):
        return False, "提交参数与待确认预约不一致，请重新确认预约目标"
    if draft.expires_at <= datetime.now():
        return False, "待确认预约已过期，请重新选择时段"
    if draft.status == "submitted":
        return False, f"这条预约已经提交（编号 {draft.reservation_id}），请勿重复提交"
    if draft.status == "submitting":
        found = find_existing(db, draft)
        if found and found.id > (draft.prior_reservation_id or 0):
            finish(db, draft, found.id)
            return False, f"这条预约已经提交（编号 {found.id}），请勿重复提交"
        return False, "预约正在提交中，请稍后查询结果，避免重复提交"
    previous = find_existing(db, draft)
    previous_id = previous.id if previous else None
    updated = db.execute(
        update(ReservationDraft)
        .where(
            ReservationDraft.id == draft.id,
            ReservationDraft.revision == draft.revision,
            ReservationDraft.status == "ready",
        )
        .values(status="submitting", prior_reservation_id=previous_id)
    )
    db.commit()
    if updated.rowcount != 1:
        return False, "预约状态已变化，请稍后查询结果"
    draft.status = "submitting"
    draft.prior_reservation_id = previous_id
    return True, ""


def finish(db: Session, draft: ReservationDraft, reservation_id: int) -> None:
    db.execute(
        update(ReservationDraft)
        .where(ReservationDraft.id == draft.id)
        .values(
            status="submitted",
            reservation_id=reservation_id,
        )
    )
    db.commit()
    draft.status = "submitted"
    draft.reservation_id = reservation_id


def release(db: Session, draft: ReservationDraft) -> None:
    db.execute(
        update(ReservationDraft)
        .where(
            ReservationDraft.id == draft.id,
            ReservationDraft.status == "submitting",
        )
        .values(status="ready")
    )
    db.commit()
    draft.status = "ready"


def discard(db: Session, draft: ReservationDraft) -> None:
    db.delete(draft)
    db.commit()
