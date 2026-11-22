"""The beginner how-to (ADR-0031 *Beginner how-to*, F23, #589): a text model shortens a catalogue
exercise's English instructions into two plain lines, stored once in `exercise_simple_texts`, a
shared table with no user column.

Its input is public catalogue text only: the name, the English instructions, the target and the
equipment, never athlete data. An exercise with no English text gets no call. A text is checked in
code before it is stored: non-empty, at most `EXERCISE_SIMPLE_HOWTO_MAX_CHARS`, and no digit the
source lacks."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# The only language written: the one the exercise card reads (G5).
SIMPLE_TEXT_LANG = "en"

_DIGITS = re.compile(r"\d+")


class SimpleHowtoDeps(BaseModel):
    """The template's variables: the exercise's public catalogue text, nothing else. Extra fields
    are refused, so no identity can ride along (ADR-0031)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exercise_name: str
    instructions: str
    target: str | None
    equipment: str | None
    # `EXERCISE_SIMPLE_HOWTO_MAX_CHARS`, so the prompt states the limit the code enforces. A
    # setting, not source text: `has_new_digit` never counts it as a number the source gives.
    max_chars: int = Field(gt=0)


class SimpleHowto(BaseModel):
    """The model's short version of the instructions."""

    text: str


class NoHowto(BaseModel):
    """The instructions don't say how the exercise is done, so nothing is written."""

    reason: str


class SimpleTextStatus(StrEnum):
    SHOWN = "shown"
    FLAGGED = "flagged"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class HowtoSource:
    """A catalogue exercise and its English text, as the template reads them."""

    exercise_id: UUID
    name: str
    instructions: str
    target: str | None
    equipment: str | None

    def deps(self, *, max_chars: int) -> SimpleHowtoDeps:
        return SimpleHowtoDeps(
            exercise_name=self.name,
            instructions=self.instructions,
            target=self.target,
            equipment=self.equipment,
            max_chars=max_chars,
        )


@dataclass(frozen=True, slots=True)
class StoredSimpleText:
    """What decides whether a stored row still stands: the source it was written from, and
    whether an admin edited it."""

    source_sha256: str
    status: SimpleTextStatus
    edited_at: datetime | None


@dataclass(frozen=True, slots=True)
class NewSimpleText:
    exercise_id: UUID
    lang: str
    text: str
    template_id: str
    template_version: int
    model: str
    source_sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SimpleHowtoDraft:
    """A text the template returned, with what wrote it, before it is checked."""

    text: str
    template_id: str
    template_version: int
    model: str


type HowtoCallFailure = Literal["timeout", "error", "rate_limited"]


@dataclass(frozen=True, slots=True)
class HowtoCallFailed:
    """The call ended without a text. A `rate_limited` call was refused by the athlete's budget
    and never sent (ADR-0029); the others wrote their `llm_calls` row."""

    reason: HowtoCallFailure


type InvalidReason = Literal["empty", "too_long", "new_digit"]


def source_sha256(instructions: str) -> str:
    """The hash of the instructions a text was written from: an import that changes them makes the
    stored text stale (ADR-0031)."""
    return hashlib.sha256(instructions.encode("utf-8")).hexdigest()


def is_current(stored: StoredSimpleText, sha256: str) -> bool:
    """A row stands when it was written from these instructions, or when an admin edited it: an
    edit is a human's text and is never rewritten."""
    return stored.edited_at is not None or stored.source_sha256 == sha256


def has_new_digit(text: str, deps: SimpleHowtoDeps) -> bool:
    """Whether a run of digits in `text` is absent from the source (name, instructions, target,
    equipment): a number the model invented."""
    source = " ".join(
        part
        for part in (deps.exercise_name, deps.instructions, deps.target, deps.equipment)
        if part
    )
    known = set(_DIGITS.findall(source))
    return any(digits not in known for digits in _DIGITS.findall(text))


def invalid_reason(text: str, deps: SimpleHowtoDeps, *, max_chars: int) -> InvalidReason | None:
    """Why a text can't be stored, or `None` when it can (ADR-0031 *Fallback*)."""
    stripped = text.strip()
    if not stripped:
        return "empty"
    if len(stripped) > max_chars:
        return "too_long"
    if has_new_digit(stripped, deps):
        return "new_digit"
    return None


@dataclass(frozen=True, slots=True)
class HowtoStored:
    text: str


@dataclass(frozen=True, slots=True)
class HowtoPresent:
    """A current or edited row already stands, or another write won the race: nothing is called
    or stored."""


@dataclass(frozen=True, slots=True)
class HowtoSkipped:
    """No catalogue exercise with English text: no call (ADR-0031)."""

    reason: Literal["no_english_text"]


type HowtoFailure = HowtoCallFailure | InvalidReason | Literal["refused"]


@dataclass(frozen=True, slots=True)
class HowtoFailed:
    """Nothing was stored; the next card tries again (ADR-0031 *Fallback*)."""

    reason: HowtoFailure


type HowtoResult = HowtoStored | HowtoPresent | HowtoSkipped | HowtoFailed
