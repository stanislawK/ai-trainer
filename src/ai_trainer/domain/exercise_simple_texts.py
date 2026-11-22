"""The beginner how-to (ADR-0031 *Beginner how-to*, F23, #589): a text model shortens a catalogue
exercise's English instructions into two plain lines, stored once in `exercise_simple_texts`, a
shared table with no user column.

Its input is public catalogue text only: the name, the English instructions, the target and the
equipment, never athlete data. An exercise with no English text gets no call. A text is checked in
code before it is stored: non-empty, at most `EXERCISE_SIMPLE_HOWTO_MAX_CHARS`, and no digit the
source lacks. A text that passes them is checked by Jev before it is stored (#591): a failed
check stores no text and counts, and the second failed check of the same source stores the row
`rejected`, after which no call is made."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Literal
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


class SimpleHowtoRetryDeps(BaseModel):
    """`exercise.simple_howto.retry`'s variables: a reply over the cap goes back to the model once
    with its length and the cap (#600). Counts only, never the text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    length: int
    limit: int


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
    """What decides whether a stored row still stands: the source it was written from, whether
    an admin edited it, and whether it holds a text. A row with no text has failed Jev's check
    once for its source (#591)."""

    source_sha256: str
    status: SimpleTextStatus
    edited_at: datetime | None
    has_text: bool
    check_failures: int


@dataclass(frozen=True, slots=True)
class NewSimpleText:
    exercise_id: UUID
    lang: str
    text: str
    template_id: str
    template_version: int
    model: str
    source_sha256: str
    # The pinned version of the Jev set that checked it (#591).
    check_version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FlaggedText:
    """What wrote a text an athlete's "Looks wrong" just flagged (ADR-0031 *The flag*, #593): the
    template and its version, for the content-free `howto_flagged` event. Never the text."""

    template_id: str
    template_version: int


@dataclass(frozen=True, slots=True)
class HowtoReviewRow:
    """A `flagged` or `rejected` how-to on the admin's list (ADR-0031 *The flag*, #594): the
    short text, its status, when an athlete flagged it, and the catalogue text it was written
    from."""

    exercise_id: UUID
    name: str
    text: str
    status: SimpleTextStatus
    flagged_at: datetime | None
    original: str


type FailedCheckOutcome = Literal["counted", "rejected", "present"]
"""What recording a failed check did: the first of its source counted, with no text kept; the
second stored the row `rejected`; or a row with a text from the same source, or an edited row,
already stood (another athlete's write passed), so nothing changed."""


@dataclass(frozen=True, slots=True)
class HowtoCheck:
    """Jev's check of a summary (ADR-0031 *Jev check*): `passed` only when both questions are at
    or above the threshold. A timeout or an error is a failed check."""

    passed: bool
    version: int


@dataclass(frozen=True, slots=True)
class HowtoCheckRefused:
    """The athlete's budget refused the check, so it never ran: nothing is stored or counted
    (ADR-0029; owner's plan-gate decision at #591)."""


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


def needs_writing(stored: StoredSimpleText | None, sha256: str) -> bool:
    """Whether a card should write the how-to: no row, a row written from other instructions, or
    a row with no text (one failed check of these instructions). An admin's edit is a human's
    text and is never rewritten; a `rejected` row keeps its text and is not written again."""
    if stored is None:
        return True
    if stored.edited_at is not None:
        return False
    return stored.source_sha256 != sha256 or not stored.has_text


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


@dataclass(frozen=True, slots=True)
class HowtoRejected:
    """The current source's summary failed Jev's check twice: the card keeps the original and no
    call is made until an admin clears it or an import changes the source (ADR-0031)."""


type HowtoFailure = HowtoCallFailure | InvalidReason | Literal["refused", "check_failed"]


@dataclass(frozen=True, slots=True)
class HowtoFailed:
    """Nothing was stored; the next card tries again (ADR-0031 *Fallback*)."""

    reason: HowtoFailure


type HowtoResult = HowtoStored | HowtoPresent | HowtoSkipped | HowtoFailed | HowtoRejected


@dataclass(frozen=True, slots=True)
class CardHowtoSource:
    """A catalogue exercise's English instructions and its stored short version, as the card
    reads them when it is drawn (#592). `text` is the stored row's text, if any."""

    instructions: str
    stored: StoredSimpleText | None
    text: str | None


@dataclass(frozen=True, slots=True)
class ShortHowto:
    """The short version replaces the clamped line, with its AI mark (#590)."""

    kind: ClassVar[Literal["short"]] = "short"
    text: str


@dataclass(frozen=True, slots=True)
class LoadHowto:
    """The clamped original now, and a request that writes the short version (ADR-0031)."""

    kind: ClassVar[Literal["load"]] = "load"


@dataclass(frozen=True, slots=True)
class KeepOriginal:
    """The clamped original, with no request: a `rejected` or flagged row, or no catalogue
    text to write from."""

    kind: ClassVar[Literal["keep"]] = "keep"


type CardHowto = ShortHowto | LoadHowto | KeepOriginal
"""What a candidate's how-to line shows on the exercise card (ADR-0031 *Beginner how-to*,
#592)."""


def card_howto(stored: StoredSimpleText | None, sha256: str, text: str | None) -> CardHowto:
    """A row that needs writing (none, stale and unedited, or text-less) loads; a flagged one,
    a current unedited `rejected` one, or one with no text keeps the original; any other row
    with a text is shown. An admin's edit is a human's text, whatever status it still carries,
    except a flag, which hides it."""
    if stored is None or needs_writing(stored, sha256):
        return LoadHowto()
    rejected = stored.status is SimpleTextStatus.REJECTED and stored.edited_at is None
    if text is None or stored.status is SimpleTextStatus.FLAGGED or rejected:
        return KeepOriginal()
    return ShortHowto(text=text)
