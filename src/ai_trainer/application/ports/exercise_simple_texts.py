"""What writing one exercise's beginner how-to reads, calls, checks and stores (ADR-0031 *Beginner
how-to*, #589, #591)."""

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.exercise_simple_texts import (
    CardHowtoSource,
    FailedCheckOutcome,
    FlaggedText,
    HowtoCallFailed,
    HowtoCheck,
    HowtoCheckRefused,
    HowtoSource,
    NewSimpleText,
    NoHowto,
    SimpleHowtoDeps,
    SimpleHowtoDraft,
    StoredSimpleText,
)


class ExerciseSimpleTextsPort(Protocol):
    """The shared catalogue and `exercise_simple_texts`. Neither holds user data."""

    async def howto_source(self, exercise_id: UUID, *, lang: str) -> HowtoSource | None:
        """The catalogue exercise with its text in `lang`; `None` for an athlete's own exercise,
        an exercise with no text in `lang`, or no such exercise."""
        ...

    async def stored(self, exercise_id: UUID, *, lang: str) -> StoredSimpleText | None: ...

    async def card_rows(
        self, exercise_ids: Collection[UUID], *, lang: str
    ) -> Mapping[UUID, CardHowtoSource]:
        """Each catalogue exercise's text in `lang` with its stored short version, by exercise
        ID, in one read (#592). An athlete's own exercise, or one with no text in `lang`, is
        left out."""
        ...

    async def write(self, text: NewSimpleText) -> bool:
        """Stores `text` `shown` unless a row with a text from the same source, or an edited row,
        already stands: the first write wins, and a stale or text-less unedited row is
        rewritten. `True` when a row was written."""
        ...

    async def record_failed_check(self, text: NewSimpleText) -> FailedCheckOutcome:
        """Counts a failed Jev check of `text` (#591). The first for its source keeps no text;
        the second stores the row `rejected` with `text`, for the admin list. A row with a text
        from the same source, or an edited row, is never changed."""
        ...

    async def flag(
        self, exercise_id: UUID, *, lang: str, flagged_at: datetime
    ) -> FlaggedText | None:
        """Sets a `shown` row with a text `flagged` at `flagged_at` (ADR-0031 *The flag*, #593)
        and returns what wrote it. A flagged, `rejected`, text-less or missing row is left as it
        is and gives `None`, so of two concurrent flags only one returns the row."""
        ...


class SimpleHowtoWriterPort(Protocol):
    """`exercise.simple_howto` through the text gateway. It never raises for a model or provider
    failure, and the gateway writes the call's `llm_calls` row under `user_id` (ADR-0018)."""

    async def write(
        self, *, user_id: UUID, deps: SimpleHowtoDeps
    ) -> SimpleHowtoDraft | NoHowto | HowtoCallFailed: ...


class SimpleHowtoCheckPort(Protocol):
    """Jev's check of a summary against its original (ADR-0031 *Jev check*, #591). It never
    raises: a timeout or an error is a failed check, and a budget refusal is its own result.
    The gateway writes the call's `llm_calls` row under `user_id` (ADR-0018)."""

    async def check(
        self, *, user_id: UUID, original: str, summary: str
    ) -> HowtoCheck | HowtoCheckRefused: ...
