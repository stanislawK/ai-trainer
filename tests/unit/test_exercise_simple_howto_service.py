"""Writing one exercise's beginner how-to (ADR-0031 *Beginner how-to*, #589, #591): a valid text
that passes Jev's check is stored once; a timeout, error, refusal or invalid text stores nothing and
logs only the reason and the exercise ID; no English text means no call; a stale row counts as
missing unless an admin edited it. A failed check stores no text and counts; the second failed
check of the same source stores `rejected`, after which no call is made."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.exercise_simple_howto import WriteSimpleHowto
from ai_trainer.domain.exercise_simple_texts import (
    FailedCheckOutcome,
    HowtoCallFailed,
    HowtoCheck,
    HowtoCheckRefused,
    HowtoFailed,
    HowtoPresent,
    HowtoRejected,
    HowtoSkipped,
    HowtoSource,
    HowtoStored,
    NewSimpleText,
    NoHowto,
    SimpleHowtoDeps,
    SimpleHowtoDraft,
    SimpleTextStatus,
    StoredSimpleText,
    needs_writing,
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
_CHECK_VERSION = 1


class FrozenClock:
    def now(self) -> datetime:
        return NOW


@dataclass
class _Row:
    text: NewSimpleText
    body: str | None
    status: SimpleTextStatus = SimpleTextStatus.SHOWN
    check_failures: int = 0


@dataclass
class InMemorySimpleTexts:
    """Implements `ExerciseSimpleTextsPort` with the repository's conditional upserts."""

    sources: dict[UUID, HowtoSource] = field(default_factory=dict)
    rows: dict[UUID, _Row] = field(default_factory=dict)
    edited: set[UUID] = field(default_factory=set)
    writes: list[NewSimpleText] = field(default_factory=list)
    failed_checks: list[NewSimpleText] = field(default_factory=list)

    async def howto_source(self, exercise_id: UUID, *, lang: str) -> HowtoSource | None:
        assert lang == "en"
        return self.sources.get(exercise_id)

    async def stored(self, exercise_id: UUID, *, lang: str) -> StoredSimpleText | None:
        row = self.rows.get(exercise_id)
        if row is None:
            return None
        return StoredSimpleText(
            source_sha256=row.text.source_sha256,
            status=row.status,
            edited_at=NOW if exercise_id in self.edited else None,
            has_text=row.body is not None,
            check_failures=row.check_failures,
        )

    async def write(self, text: NewSimpleText) -> bool:
        stored = await self.stored(text.exercise_id, lang=text.lang)
        if stored is not None and not needs_writing(stored, text.source_sha256):
            return False
        failures = (
            stored.check_failures
            if stored is not None and stored.source_sha256 == text.source_sha256
            else 0
        )
        self.rows[text.exercise_id] = _Row(text, text.text, check_failures=failures)
        self.writes.append(text)
        return True

    async def record_failed_check(self, text: NewSimpleText) -> FailedCheckOutcome:
        self.failed_checks.append(text)
        stored = await self.stored(text.exercise_id, lang=text.lang)
        if stored is not None and not needs_writing(stored, text.source_sha256):
            return "present"
        if stored is not None and stored.source_sha256 == text.source_sha256:
            self.rows[text.exercise_id] = _Row(
                text, text.text, SimpleTextStatus.REJECTED, stored.check_failures + 1
            )
            return "rejected"
        self.rows[text.exercise_id] = _Row(text, None, check_failures=1)
        return "counted"


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


@dataclass
class ScriptedCheck:
    """Implements `SimpleHowtoCheckPort`; returns the next of `replies` (the last repeats)."""

    replies: list[HowtoCheck | HowtoCheckRefused] = field(
        default_factory=lambda: [HowtoCheck(passed=True, version=_CHECK_VERSION)]
    )
    calls: list[tuple[UUID, str, str]] = field(default_factory=list)

    async def check(
        self, *, user_id: UUID, original: str, summary: str
    ) -> HowtoCheck | HowtoCheckRefused:
        self.calls.append((user_id, original, summary))
        return self.replies[min(len(self.calls), len(self.replies)) - 1]


_FAIL = HowtoCheck(passed=False, version=_CHECK_VERSION)


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
    texts: InMemorySimpleTexts,
    writer: ScriptedWriter,
    *,
    check: ScriptedCheck | None = None,
    max_chars: int = 160,
) -> WriteSimpleHowto:
    return WriteSimpleHowto(
        texts=texts,
        writer=writer,
        check=check or ScriptedCheck(),
        clock=FrozenClock(),
        max_chars=max_chars,
    )


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
        max_chars=160,
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
            check_version=_CHECK_VERSION,
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
    assert texts.rows[exercise].text.source_sha256 == source_sha256(_INSTRUCTIONS)


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
    assert texts.rows[exercise].body == "Old short text."


# --- Jev's check (#591) --------------------------------------------------------------------


# AC 1: a passing summary is stored `shown`, with the check's version.
async def test_passing_check_stores_shown_with_check_version() -> None:
    exercise, athlete = uuid4(), uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise)})
    check = ScriptedCheck()

    result = await _service(texts, ScriptedWriter(_draft()), check=check).write(
        user_id=athlete, exercise_id=exercise
    )

    assert result == HowtoStored(text=_GOOD)
    assert check.calls == [(athlete, _INSTRUCTIONS, _GOOD)]
    row = texts.rows[exercise]
    assert (row.status, row.body, row.text.check_version) == (
        SimpleTextStatus.SHOWN,
        _GOOD,
        _CHECK_VERSION,
    )


async def test_an_invalid_text_is_never_checked() -> None:
    exercise = uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise)})
    check = ScriptedCheck()

    await _service(texts, ScriptedWriter(_draft("")), check=check).write(
        user_id=uuid4(), exercise_id=exercise
    )

    assert check.calls == []


async def test_first_failed_check_stores_no_text_and_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exercise = uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise)})
    service = _service(texts, ScriptedWriter(_draft()), check=ScriptedCheck([_FAIL]))

    with caplog.at_level(logging.INFO, logger="ai_trainer.application.exercise_simple_howto"):
        result = await service.write(user_id=uuid4(), exercise_id=exercise)

    assert result == HowtoFailed(reason="check_failed")
    assert texts.writes == []
    row = texts.rows[exercise]
    assert (row.body, row.check_failures) == (None, 1)
    [record] = caplog.records
    assert record.getMessage() == (
        f"simple how-to not stored: reason=check_failed exercise={exercise}"
    )


async def test_a_once_failed_row_is_written_again_on_the_next_card() -> None:
    exercise = uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise)})
    check = ScriptedCheck([_FAIL, HowtoCheck(passed=True, version=_CHECK_VERSION)])
    writer = ScriptedWriter(_draft())

    await _service(texts, writer, check=check).write(user_id=uuid4(), exercise_id=exercise)
    second = await _service(texts, writer, check=check).write(user_id=uuid4(), exercise_id=exercise)

    assert second == HowtoStored(text=_GOOD)
    assert len(writer.calls) == 2
    assert texts.rows[exercise].status is SimpleTextStatus.SHOWN


# AC 2: a second failure stores `rejected`; a later write makes no call.
async def test_second_failed_check_stores_rejected_and_later_write_calls_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exercise = uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise)})
    check = ScriptedCheck([_FAIL])
    writer = ScriptedWriter(_draft())

    first = await _service(texts, writer, check=check).write(user_id=uuid4(), exercise_id=exercise)
    with caplog.at_level(logging.INFO, logger="ai_trainer.application.exercise_simple_howto"):
        second = await _service(texts, writer, check=check).write(
            user_id=uuid4(), exercise_id=exercise
        )
    third = await _service(texts, writer, check=check).write(user_id=uuid4(), exercise_id=exercise)

    assert first == HowtoFailed(reason="check_failed")
    assert second == HowtoRejected()
    assert third == HowtoRejected()
    assert len(writer.calls) == len(check.calls) == 2
    row = texts.rows[exercise]
    assert (row.status, row.body, row.check_failures, row.text.check_version) == (
        SimpleTextStatus.REJECTED,
        _GOOD,
        2,
        _CHECK_VERSION,
    )
    [record] = caplog.records
    assert record.getMessage() == f"simple how-to rejected: exercise={exercise}"


async def test_a_failed_check_that_loses_the_race_is_present() -> None:
    exercise = uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise)})

    class PassedElsewhere(InMemorySimpleTexts):
        async def record_failed_check(self, text: NewSimpleText) -> FailedCheckOutcome:
            return "present"

    raced = PassedElsewhere(sources=texts.sources)
    service = _service(raced, ScriptedWriter(_draft()), check=ScriptedCheck([_FAIL]))

    assert await service.write(user_id=uuid4(), exercise_id=exercise) == HowtoPresent()


# AC 3: a Jev timeout or error counts as a fail (the adapter returns a failed check for both).
async def test_check_timeout_counts_as_failed_check() -> None:
    exercise = uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise)})
    # `JevSimpleHowtoCheck` turns a timeout into this (tests/unit/test_simple_howto_check.py).
    timed_out = ScriptedCheck([HowtoCheck(passed=False, version=_CHECK_VERSION)])

    result = await _service(texts, ScriptedWriter(_draft()), check=timed_out).write(
        user_id=uuid4(), exercise_id=exercise
    )

    assert result == HowtoFailed(reason="check_failed")
    assert texts.rows[exercise].check_failures == 1


async def test_a_budget_refused_check_stores_and_counts_nothing() -> None:
    """Owner's plan-gate decision (#591): a refused check is not a failed one."""
    exercise = uuid4()
    texts = InMemorySimpleTexts(sources={exercise: _source(exercise)})
    service = _service(texts, ScriptedWriter(_draft()), check=ScriptedCheck([HowtoCheckRefused()]))

    result = await service.write(user_id=uuid4(), exercise_id=exercise)

    assert result == HowtoFailed(reason="rate_limited")
    assert texts.rows == {}
    assert texts.failed_checks == []


def _stored(**changes: object) -> StoredSimpleText:
    values: dict[str, object] = {
        "source_sha256": source_sha256(_INSTRUCTIONS),
        "status": SimpleTextStatus.SHOWN,
        "edited_at": None,
        "has_text": True,
        "check_failures": 0,
    }
    return StoredSimpleText(**(values | changes))  # type: ignore[arg-type]  # test values


def test_needs_writing() -> None:
    sha = source_sha256(_INSTRUCTIONS)

    assert needs_writing(None, sha)
    assert not needs_writing(_stored(), sha)
    assert needs_writing(_stored(source_sha256="0" * 64), sha)
    assert needs_writing(_stored(has_text=False, check_failures=1), sha)
    assert not needs_writing(_stored(status=SimpleTextStatus.REJECTED, check_failures=2), sha)
    assert not needs_writing(_stored(source_sha256="0" * 64, edited_at=NOW), sha)
