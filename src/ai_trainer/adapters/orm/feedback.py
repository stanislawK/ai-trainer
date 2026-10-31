import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from ai_trainer.adapters.orm.base import Base


class FeedbackEventOrm(Base):
    """One content-free row per signal that an athlete corrected or declined what the app
    made (ADR-0034). It holds no message text and no value, only the kind, the template and the
    names of the fields involved; it goes with the user (G7)."""

    __tablename__ = "feedback_events"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('draft_edited', 'draft_discarded', 'session_undone', 'choice_answered', "
            "'decision_overridden')",
            name="feedback_events_kind",
        ),
        Index("ix_feedback_events_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(nullable=False)
    template_id: Mapped[str | None] = mapped_column(nullable=True)
    template_version: Mapped[int | None] = mapped_column(nullable=True)
    fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    # timestamptz, UTC (ADR-0004 invariant 5).
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
