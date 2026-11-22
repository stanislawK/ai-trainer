"""What writing one exercise's beginner how-to reads, calls and stores (ADR-0031 *Beginner how-to*,
#589)."""

from typing import Protocol
from uuid import UUID

from ai_trainer.domain.exercise_simple_texts import (
    HowtoCallFailed,
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

    async def write(self, text: NewSimpleText) -> bool:
        """Stores `text` unless a row written from the same source, or an edited row, already
        stands: the first write wins, and a stale unedited row is rewritten. `True` when a row
        was written."""
        ...


class SimpleHowtoWriterPort(Protocol):
    """`exercise.simple_howto` through the text gateway. It never raises for a model or provider
    failure, and the gateway writes the call's `llm_calls` row under `user_id` (ADR-0018)."""

    async def write(
        self, *, user_id: UUID, deps: SimpleHowtoDeps
    ) -> SimpleHowtoDraft | NoHowto | HowtoCallFailed: ...
