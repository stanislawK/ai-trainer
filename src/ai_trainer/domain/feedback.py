"""Feedback events (ADR-0034, PRD-0010 G6): one content-free row per signal that an athlete
corrected or declined what the app made. A row holds the kind, the template it concerns and
the names of the fields involved, never a message or a value."""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FeedbackKind(StrEnum):
    DRAFT_EDITED = "draft_edited"
    DRAFT_DISCARDED = "draft_discarded"
    SESSION_UNDONE = "session_undone"
    CHOICE_ANSWERED = "choice_answered"
    # Nothing writes it until the Jev pick (ADR-0034); the table's check admits it already.
    DECISION_OVERRIDDEN = "decision_overridden"


class NewFeedbackEvent(BaseModel):
    """What a signal records. `fields` are field names (`reps`, `rpe`) or, for a choice, the
    question's purpose; the template is that of the draft or reply the athlete answered, `None`
    for a draft made before drafts kept theirs."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    kind: FeedbackKind
    template_id: str | None = None
    template_version: int | None = None
    fields: Sequence[str] = ()
    created_at: datetime


class FeedbackEvent(NewFeedbackEvent):
    id: UUID


class FeedbackSignal(BaseModel):
    """A stored event as the report reads it: no user, no ID."""

    model_config = ConfigDict(frozen=True)

    kind: FeedbackKind
    template_id: str | None = None
    template_version: int | None = None
    fields: Sequence[str] = ()


class FeedbackCount(BaseModel):
    """How many events of `kind` named `field` under a template; `field` is `None` for an
    event that names none, as a discard."""

    model_config = ConfigDict(frozen=True)

    kind: FeedbackKind
    template_id: str | None
    field: str | None
    count: int
