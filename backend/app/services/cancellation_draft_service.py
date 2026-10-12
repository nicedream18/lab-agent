"""取消候选的持久化及原子提交。"""

from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.cancellation_draft import CancellationDraft
from app.models.reservation import Reservation

TTL = timedelta(minutes=20)


def load(db: Session, user_id: int, conversation_id: str) -> CancellationDraft | None:
    if not conversation_id:
        return None
    draft = db.query(CancellationDraft).filter_by(
        user_id=user_id, conversation_id=conversation_id
    ).first()
    if draft is None or draft.expires_at <= datetime.now():
        return None
    return draft


def save(db: Session, user_id: int, conversation_id: str, ids: list[int]) -> None:
    ids = list(dict.fromkeys(ids))
    if not ids:
        return
    draft = db.query(CancellationDraft).filter_by(
        user_id=user_id, conversation_id=conversation_id
    ).first()
    if draft is None:
        draft = CancellationDraft(user_id=user_id, conversation_id=conversation_id)
        db.add(draft)
    draft.reservation_ids = ids
    draft.status = "ready"
    draft.revision = (draft.revision or 0) + 1
    draft.expires_at = datetime.now() + TTL
    db.commit()


def discard(db: Session, draft: CancellationDraft) -> None:
    db.delete(draft)
    db.commit()


def cancel_selected(
    db: Session, user_id: int, conversation_id: str, revision: int, ids: list[int]
) -> dict:
    """先校验整组，再在单一事务内取消；任一失败都不改变任何预约。"""
    ids = list(dict.fromkeys(ids))
    draft = load(db, user_id, conversation_id)
    if draft is None or draft.status != "ready" or draft.revision != revision:
        return {"ok": False, "error": "待取消目标已过期或已处理，请重新查询预约"}
    if not ids or any(item not in draft.reservation_ids for item in ids):
        return {"ok": False, "error": "取消目标不属于本会话待确认的预约"}
    try:
        rows = db.query(Reservation).filter(
            Reservation.user_id == user_id, Reservation.id.in_(ids)
        ).all()
        if len(rows) != len(ids) or any(row.status != 0 for row in rows):
            db.rollback()
            return {"ok": False, "error": "其中一条预约已不存在或不可取消，整组未取消"}
        claimed = db.execute(
            update(CancellationDraft)
            .where(
                CancellationDraft.id == draft.id,
                CancellationDraft.revision == revision,
                CancellationDraft.status == "ready",
                CancellationDraft.expires_at > datetime.now(),
            )
            .values(status="submitted")
        )
        if claimed.rowcount != 1:
            db.rollback()
            return {"ok": False, "error": "取消请求已处理，请重新查询预约"}
        for reservation_id in ids:
            updated = db.execute(
                update(Reservation)
                .where(
                    Reservation.id == reservation_id,
                    Reservation.user_id == user_id,
                    Reservation.status == 0,
                )
                .values(status=3)
            )
            if updated.rowcount != 1:
                db.rollback()
                return {"ok": False, "error": "预约状态发生变化，整组未取消"}
        result = [
            {
                "reservation_id": row.id,
                "lab_name": row.lab.name if row.lab else None,
                "date": row.date,
                "start_time": row.start_time,
                "end_time": row.end_time,
                "status": 3,
                "status_text": "已取消",
            }
            for row in rows
        ]
        db.commit()
        return {"ok": True, "cancelled": result, "message": "预约已取消"}
    except Exception:
        db.rollback()
        raise
