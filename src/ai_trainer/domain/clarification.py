"""Structured clarification (PRD-0004 B17, B23, F9, ADR-0015, ADR-0022): a question the athlete
answers by tapping one of a few concrete options. Asking writes nothing but the question itself
(ADR-0015 invariant 3), and the answer is option IDs, never text (invariant 4)."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# What is being asked, as stored in `clarifications.purpose` (ADR-0022): text, not an enum.
SPORT_PURPOSE = "sport"
# The fixed reply whose line sits above a sport question's choice card (ADR-0008): the chat
# finds the card's message by it, as it finds a draft's by its extraction template.
SPORT_QUESTION_TEMPLATE_ID = "sport_question"
# Whether a log beyond the merge window, on its session's own day, is the same session (B16,
# ADR-0014, ticket #166). It blocks a stored draft and is asked on the draft's own card.
SAME_SESSION_PURPOSE = "same_session"
# How to read a log its extraction couldn't read safely (ticket #164): asked by the sport's
# extraction template, whose sport the question's document names.
READING_PURPOSE = "reading"
# The fixed reply above a reading question's choice card, as `SPORT_QUESTION_TEMPLATE_ID` is.
READING_QUESTION_TEMPLATE_ID = "reading_question"
# The same-session question's "New session" option; its other option's ID is the session's.
NEW_SESSION_OPTION = "new"
# The copy keys of its two options, in `components/session_copy.html`.
SAME_SESSION_ADD_COPY_KEY = "same_session_add"
SAME_SESSION_NEW_COPY_KEY = "same_session_new"
# Which exercise a typed name is (B37, ADR-0031, ticket #397). Like the same-session question it
# blocks a stored draft and is asked on the draft's own card, one name at a time.
EXERCISE_PURPOSE = "exercise"
# The exercise question's "Keep my name" option; its others' IDs are the candidate exercises'.
KEEP_NAME_OPTION = "keep"

type ClarificationMode = Literal["single", "multi"]
type ClarificationStatus = Literal["open", "answered", "abandoned"]


class ChoiceOption(BaseModel):
    """One tappable answer. `id` is what the athlete's tap sends back; `copy_key` names the
    label and detail a template shows, as a sport's `label_key` does (G5: no user-facing text
    in Python)."""

    model_config = ConfigDict(frozen=True)

    id: str
    copy_key: str
    # Values, not copy, that the option's copy shows, e.g. a session's start time `time`.
    params: dict[str, str] = Field(default_factory=dict)


class Clarification(BaseModel):
    """The `clarifications.clarification` document: the question as written in the persona's
    voice, its options, and the intent it blocks (`pending`). One option is enough only beside
    the free-text escape, which is then the second route (ADR-0015 invariant 2, ticket #397).
    `sport` is the sport whose extraction asked, for a question its tap resumes in that sport
    (ticket #164). `subject` is the name as typed that an exercise question asks about, and
    `typed_name` the name the athlete gave it in the card's name field (ticket #402): text the
    answered row shows, never read by a model. The answer is still the `keep` option's ID."""

    model_config = ConfigDict(frozen=True)

    question: str
    options: tuple[ChoiceOption, ...] = Field(min_length=1)
    mode: ClarificationMode = "single"
    allow_free_text: bool = True
    pending: str
    sport: str | None = None
    subject: str | None = None
    typed_name: str | None = None

    @model_validator(mode="after")
    def _has_a_second_route(self) -> Self:
        if len(self.options) < 2 and not self.allow_free_text:
            raise ValueError("a question with one option needs the free-text escape")
        return self

    @property
    def card_purpose(self) -> str:
        """What this question is, when it is asked on a draft's card: one about a typed name is
        an exercise question, any other the same-session one."""
        return SAME_SESSION_PURPOSE if self.subject is None else EXERCISE_PURPOSE


class StoredClarification(BaseModel):
    """A `clarifications` row. `message_id` is the athlete's message the question blocks, and
    `draft_id` its draft when one is stored."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    conversation_id: UUID
    message_id: UUID
    purpose: str
    clarification: Clarification
    status: ClarificationStatus
    # The stored draft the question blocks, when it blocks one (ADR-0022).
    draft_id: UUID | None = None
    answer_option_ids: tuple[str, ...] | None = None
    created_at: datetime


class ClarificationNotFoundError(Exception):
    """Raised for a clarification that doesn't exist or isn't the user's."""


class ClarificationResolvedError(Exception):
    """Raised for an answer to a clarification that is no longer open."""


class UnknownOptionError(Exception):
    """Raised when an answer names an option ID the question never offered, or the wrong
    number of them for its mode (ADR-0015 invariant 4)."""
