"""会话内待确认取消的预约编号，编号仅从数据库取得。"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CancellationDraft(Base):
    __tablename__ = "cancellation_draft"
    __table_args__ = (UniqueConstraint("user_id", "conversation_id"),)

    user_id: Mapped[int] = mapped_column(Integer, index=True)
    conversation_id: Mapped[str] = mapped_column(String(64))
    reservation_ids: Mapped[list[int]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="ready")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
