"""Structured clarification (PRD-0004 B17, B23, F9, ADR-0015, ADR-0022): a question the athlete
answers by tapping one of a few concrete options. Asking writes nothing but the question itself
(ADR-0015 invariant 3), and the answer is option IDs, never text (invariant 4)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# What is being asked, as stored in `clarifications.purpose` (ADR-0022): text, not an enum.
SPORT_PURPOSE = "sport"
# The fixed reply whose line sits above a sport question's choice card (ADR-0008): the chat
# finds the card's message by it, as it finds a draft's by its extraction template.
SPORT_QUESTION_TEMPLATE_ID = "sport_question"

type ClarificationMode = Literal["single", "multi"]
type ClarificationStatus = Literal["open", "answered", "abandoned"]


class ChoiceOption(BaseModel):
    """One tappable answer. `id` is what the athlete's tap sends back; `copy_key` names the
    label and detail a template shows, as a sport's `label_key` does (G5: no user-facing text
    in Python)."""

    model_config = ConfigDict(frozen=True)

    id: str
    copy_key: str


class Clarification(BaseModel):
    """The `clarifications.clarification` document: the question as written in the persona's
    voice, 2 or more options, and the intent it blocks (`pending`)."""

    model_config = ConfigDict(frozen=True)

    question: str
    options: tuple[ChoiceOption, ...] = Field(min_length=2)
    mode: ClarificationMode = "single"
    allow_free_text: bool = True
    pending: str


class StoredClarification(BaseModel):
    """A `clarifications` row. `message_id` is the athlete's message the question blocks."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    conversation_id: UUID
    message_id: UUID
    purpose: str
    clarification: Clarification
    status: ClarificationStatus
    answer_option_ids: tuple[str, ...] | None = None
    created_at: datetime


class ClarificationNotFoundError(Exception):
    """Raised for a clarification that doesn't exist or isn't the user's."""


class ClarificationResolvedError(Exception):
    """Raised for an answer to a clarification that is no longer open."""


class UnknownOptionError(Exception):
    """Raised when an answer names an option ID the question never offered, or the wrong
    number of them for its mode (ADR-0015 invariant 4)."""
