"""Training-session drafts (PRD-0004 B1, F2, ADR-0022): what an extraction made of a log, held
until the athlete confirms it. Nothing here is a saved session (ADR-0022 invariant 3)."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from ai_trainer.domain.sports.base import DateSource

type DraftStatus = Literal["pending", "confirmed", "discarded"]


class SessionDraft(BaseModel):
    """The `session_drafts.draft` document: the envelope fields, how the date was found
    (ADR-0014) and the sport payload as JSON. Only the sport's plugin model reads the payload
    (ADR-0022), so it stays a plain object here."""

    model_config = ConfigDict(frozen=True)

    occurred_on: date
    date_source: DateSource
    # The IANA zone `occurred_on` was resolved in (ADR-0022).
    timezone: str
    started_at: datetime | None = None
    duration_min: int | None = Field(default=None, ge=1, le=1440)
    rpe: int | None = Field(default=None, ge=1, le=10)
    feeling: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    payload: dict[str, JsonValue]


class StoredDraft(BaseModel):
    """A `session_drafts` row. `message_id` is the athlete's log it came from."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    conversation_id: UUID
    message_id: UUID
    sport_id: str
    draft: SessionDraft
    payload_version: int
    status: DraftStatus
    created_at: datetime
