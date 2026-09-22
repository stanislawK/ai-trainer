import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, ForeignKey, Numeric, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserOrm(Base):
    """Minimal placeholder: #13 (sign in with Google) adds sub/email/name/status/timezone."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class LlmCallOrm(Base):
    """One row per gateway call, on success, timeout and error alike (ADR-0018)."""

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_id: Mapped[str] = mapped_column(nullable=False)
    template_version: Mapped[int] = mapped_column(nullable=False)
    model: Mapped[str] = mapped_column(nullable=False)
    input_tokens: Mapped[int] = mapped_column(nullable=False)
    output_tokens: Mapped[int] = mapped_column(nullable=False)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(nullable=False)
    outcome: Mapped[str] = mapped_column(nullable=False)
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
