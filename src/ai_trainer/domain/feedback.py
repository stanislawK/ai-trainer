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
    # An athlete's "Looks wrong" on a beginner how-to, with its template and version (ADR-0031
    # *The flag*, #593).
    HOWTO_FLAGGED = "howto_flagged"
    # An athlete's "Looks wrong" on a picture drawn on the exercise card, with no template and no
    # field: a stored picture records no template version (ADR-0031 *Picture on the card*, #644).
    PICTURE_FLAGGED = "picture_flagged"


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
    """How many events of `kind` named `field` under a template version; `field` is `None` for
    an event that names none, as a discard or a flag, and `template_version` is `None`
    for a row that has none."""

    model_config = ConfigDict(frozen=True)

    kind: FeedbackKind
    template_id: str | None
    template_version: int | None = None
    field: str | None
    count: int

    @property
    def template(self) -> str | None:
        """The template as the report prints it: `<id>@v<N>` when the row has a version (#593),
        the bare ID when it has none."""
        if self.template_id is None or self.template_version is None:
            return self.template_id
        return f"{self.template_id}@v{self.template_version}"
