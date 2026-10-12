"""会话内待提交预约。只保存一个目标，避免从聊天文本猜写库参数。"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ReservationDraft(Base):
    __tablename__ = "reservation_draft"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "conversation_id", name="uq_reservation_draft_owner"
        ),
    )

    user_id: Mapped[int] = mapped_column(Integer, index=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    target: Mapped[dict] = mapped_column(JSON, default=dict)
    # 用户先前明确要求预约，补齐最后一个槽位即可提交。
    requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # ready -> submitting -> submitted；submitting 防止并行请求重复落库。
    status: Mapped[str] = mapped_column(String(20), default="ready")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    reservation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 提交前已存在的同目标预约；只把更新的编号视作本次提交结果。
    prior_reservation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
