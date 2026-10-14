"""Training-session drafts (PRD-0004 B1, F2, ADR-0022): what an extraction made of a log, held
until the athlete confirms it. Nothing here is a saved session (ADR-0022 invariant 3)."""

from datetime import date, datetime, timedelta
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


class SessionSpan(BaseModel):
    """A saved session as continuity sees it (B16, ADR-0014): its day and when its first and
    latest logs were sent. The merge window runs from `last_logged_at` (ADR-0022)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    occurred_on: date
    first_logged_at: datetime
    last_logged_at: datetime


def joins_day(draft: SessionDraft, occurred_on: date) -> bool:
    """Whether the draft can belong to a session on `occurred_on`: a log that names another
    day never does, while one assumed to be today does, so a session running past midnight
    stays one (ADR-0014 Consequences, ticket #165)."""
    return draft.date_source == "assumed_today" or draft.occurred_on == occurred_on


def joins_session(
    draft: SessionDraft, logged_at: datetime, session: SessionSpan, window: timedelta
) -> bool:
    """Whether a log sent at `logged_at` is offered as an addition to `session` (B16,
    ADR-0014): sent within `window` of the session's latest log, the boundary included, and on
    a day it can belong to."""
    return abs(logged_at - session.last_logged_at) <= window and joins_day(
        draft, session.occurred_on
    )


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
    # The session a confirm produced or added to; `None` until then, and again after an undo.
    session_id: UUID | None = None
    # The session B16 offers to add this draft to, while it still exists (ticket #165).
    extends: SessionSpan | None = None
    created_at: datetime


class DraftNotFoundError(Exception):
    """No draft of this user has the ID: another user's draft is as unknown as a missing one."""


class DraftDiscardedError(Exception):
    """The draft was discarded, so it can't be confirmed (ADR-0022)."""


class SessionNotFoundError(Exception):
    """No saved session of this user has the ID: another user's session is as unknown as a
    removed one."""


class DraftConfirmedError(Exception):
    """The draft was confirmed and its session saved, so it can't be discarded (ADR-0022)."""


class DraftEditError(Exception):
    """The athlete's edit of a draft has values its models reject (F2, ticket #158). `fields`
    names each one as the edit form does, e.g. `rpe` or `activities.0.count`; the draft is left
    as it was."""

    def __init__(self, fields: frozenset[str]) -> None:
        super().__init__(f"invalid fields: {sorted(fields)}")
        self.fields = fields
