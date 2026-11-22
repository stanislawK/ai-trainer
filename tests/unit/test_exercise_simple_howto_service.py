"""Writing one exercise's beginner how-to (ADR-0031 *Beginner how-to*, #589): a valid text is
stored once; a timeout, error, refusal or invalid text stores nothing and logs only the reason and
the exercise ID; no English text means no call; a stale row counts as missing unless an admin
edited it."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.exercise_simple_howto import WriteSimpleHowto
from ai_trainer.domain.exercise_simple_texts import (
    HowtoCallFailed,
    HowtoFailed,
    HowtoPresent,
    HowtoSkipped,
    HowtoSource,
    HowtoStored,
    NewSimpleText,
    NoHowto,
    SimpleHowtoDeps,
    SimpleHowtoDraft,
    SimpleTextStatus,
    StoredSimpleText,
    is_current,
    source_sha256,
)

NOW = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
_INSTRUCTIONS = (
    "Lie flat on your back with your knees bent. Lift your upper body until your torso is at a "
    "45-degree angle. Keep your back straight."
)
_GOOD = (
    "Lie on your back, knees bent, and lift your upper body to 45 degrees. Keep your back straight."
)


class FrozenClock:
    def now(self) -> datetime:
        return NOW


@dataclass
class InMemorySimpleTexts:
    """Implements `ExerciseSimpleTextsPort` with the repository's conditional upsert."""

    sources: dict[UUID, HowtoSource] = field(default_factory=dict)
    rows: dict[UUID, NewSimpleText] = field(default_factory=dict)
    edited: set[UUID] = field(default_factory=set)
    writes: list[NewSimpleText] = field(default_factory=list)

    async def howto_source(self, exercise_id: UUID, *, lang: str) -> HowtoSource | None:
        assert lang == "en"
        return self.sources.get(exercise_id)

    async def stored(self, exercise_id: UUID, *, lang: str) -> StoredSimpleText | None:
        row = self.rows.get(exercise_id)
        if row is None:
            return None
        return StoredSimpleText(
            source_sha256=row.source_sha256,
            status=SimpleTextStatus.SHOWN,
            edited_at=NOW if exercise_id in self.edited else None,
        )

    async def write(self, text: NewSimpleText) -> bool:
        stored = await self.stored(text.exercise_id, lang=text.lang)
        if stored is not None and is_current(stored, text.source_sha256):
            return False
        self.rows[text.exercise_id] = text
        self.writes.append(text)
        return True


@dataclass
class ScriptedWriter:
    """Implements `SimpleHowtoWriterPort`; returns `reply` and remembers each call."""

    reply: SimpleHowtoDraft | NoHowto | HowtoCallFailed
    calls: list[tuple[UUID, SimpleHowtoDeps]] = field(default_factory=list)

    async def write(
        self, *, user_id: UUID, deps: SimpleHowtoDeps
    ) -> SimpleHowtoDraft | NoHowto | HowtoCallFailed:
        self.calls.append((user_id, deps))
        return self.reply


def _draft(text: str = _GOOD) -> SimpleHowtoDraft:
    return SimpleHowtoDraft(
        text=text, template_id="exercise.simple_howto", template_version=1, model="test/model"
    )


def _source(exercise_id: UUID, instructions: str = _INSTRUCTIONS) -> HowtoSource:
    return HowtoSource(
        exercise_id=exercise_id,
        name="3/4 Sit-Up",
        instructions=instructions,
        target="abs",
        equipment="body weight",
    )


def _service(
    texts: InMemorySimpleTexts, writer: ScriptedWriter, *, max_chars: int = 160
) -> WriteSimpleHowto:
    return WriteSimpleHowto(texts=texts, writer=writer, clock=FrozenClock(), max_chars=max_chars)


# AC: a valid output is stored once; a second write stores nothing.
async def test_valid_howto_is_stored_once_and_a_second_write_makes_no_call() -> None:
    exercise, athlete = uuid4(), uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise)})
    writer = ScriptedWriter(_draft())
    service = _service(texts, writer)

    first = await service.write(user_id=athlete, exercise_id=exercise)
    second = await service.write(user_id=uuid4(), exercise_id=exercise)

    assert first == HowtoStored(text=_GOOD)
    assert second == HowtoPresent()
    assert [user for user, _ in writer.calls] == [athlete]
    assert writer.calls[0][1] == SimpleHowtoDeps(
        exercise_name="3/4 Sit-Up",
        instructions=_INSTRUCTIONS,
        target="abs",
        equipment="body weight",
    )
    assert texts.writes == [
        NewSimpleText(
            exercise_id=exercise,
            lang="en",
            text=_GOOD,
            template_id="exercise.simple_howto",
            template_version=1,
            model="test/model",
            source_sha256=source_sha256(_INSTRUCTIONS),
            created_at=NOW,
        )
    ]


async def test_a_write_that_loses_the_race_is_present() -> None:
    exercise = uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise)})

    class RaceLost(InMemorySimpleTexts):
        async def write(self, text: NewSimpleText) -> bool:
            return False

    lost = RaceLost(sources=texts.sources)

    result = await _service(lost, ScriptedWriter(_draft())).write(
        user_id=uuid4(), exercise_id=exercise
    )

    assert result == HowtoPresent()


# AC: a timeout, error, refusal or invalid output stores nothing.
@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (HowtoCallFailed(reason="timeout"), "timeout"),
        (HowtoCallFailed(reason="error"), "error"),
        (HowtoCallFailed(reason="rate_limited"), "rate_limited"),
        (NoHowto(reason="it doesn't say how"), "refused"),
        (_draft(""), "empty"),
        (_draft("   \n "), "empty"),
        (_draft("x" * 161), "too_long"),
        (_draft("Lie back and do 3 sets of 12."), "new_digit"),
    ],
    ids=[
        "timeout",
        "error",
        "rate_limited",
        "refused",
        "empty",
        "whitespace",
        "too_long",
        "new_digit",
    ],
)
async def test_failed_call_or_invalid_output_stores_nothing(
    reply: SimpleHowtoDraft | NoHowto | HowtoCallFailed,
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exercise = uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise)})

    with caplog.at_level(logging.INFO, logger="ai_trainer.application.exercise_simple_howto"):
        result = await _service(texts, ScriptedWriter(reply)).write(
            user_id=uuid4(), exercise_id=exercise
        )

    assert result == HowtoFailed(reason=reason)  # type: ignore[arg-type]  # parametrized literal
    assert texts.writes == []
    [record] = caplog.records
    assert record.getMessage() == f"simple how-to not stored: reason={reason} exercise={exercise}"


# AC: no English text, no call.
async def test_no_english_text_makes_no_call() -> None:
    writer = ScriptedWriter(_draft())
    texts = InMemorySimpleTexts()

    result = await _service(texts, writer).write(user_id=uuid4(), exercise_id=uuid4())

    assert result == HowtoSkipped(reason="no_english_text")
    assert writer.calls == []
    assert texts.writes == []


# AC: a row with a stale `source_sha256` counts as missing, unless `edited_at` is set.
async def test_a_stale_row_is_rewritten() -> None:
    exercise = uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise, "Old instructions.")})
    await _service(texts, ScriptedWriter(_draft("Old short text."))).write(
        user_id=uuid4(), exercise_id=exercise
    )
    texts.sources[exercise] = _source(exercise)
    writer = ScriptedWriter(_draft())

    result = await _service(texts, writer).write(user_id=uuid4(), exercise_id=exercise)

    assert result == HowtoStored(text=_GOOD)
    assert len(writer.calls) == 1
    assert texts.rows[exercise].source_sha256 == source_sha256(_INSTRUCTIONS)


async def test_a_stale_row_an_admin_edited_is_kept_with_no_call() -> None:
    exercise = uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise, "Old instructions.")})
    await _service(texts, ScriptedWriter(_draft("Old short text."))).write(
        user_id=uuid4(), exercise_id=exercise
    )
    texts.edited.add(exercise)
    texts.sources[exercise] = _source(exercise)
    writer = ScriptedWriter(_draft())

    result = await _service(texts, writer).write(user_id=uuid4(), exercise_id=exercise)

    assert result == HowtoPresent()
    assert writer.calls == []
    assert texts.rows[exercise].text == "Old short text."
