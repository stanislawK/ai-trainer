"""The admin's review of flagged and rejected how-tos (ADR-0031, #594): an edit is stripped and
kept to `EXERCISE_SIMPLE_HOWTO_MAX_CHARS`, stored with the clock's time; an empty or over-long one
stores nothing; a row no longer listed is `None`; Clear names the exercise."""

from typing import Literal
from uuid import uuid4

import pytest

from ai_trainer.application.exercise_simple_howto import (
    HowtoCleared,
    HowtoEdited,
    HowtoEditInvalid,
    HowtoReview,
)
from ai_trainer.domain.exercise_simple_texts import HowtoReviewRow, SimpleTextStatus
from tests.simple_howto_support import NOW, FrozenClock, MemoryHowtoReview

_MAX = 20


def _row(name: str = "Zz Squat") -> HowtoReviewRow:
    return HowtoReviewRow(
        exercise_id=uuid4(),
        name=name,
        text="Sit down, stand up.",
        status=SimpleTextStatus.FLAGGED,
        flagged_at=NOW,
        original="Squat down and stand up.",
    )


def _review(*rows: HowtoReviewRow) -> tuple[HowtoReview, MemoryHowtoReview]:
    store = MemoryHowtoReview(rows={row.exercise_id: row for row in rows})
    return HowtoReview(texts=store, clock=FrozenClock(), max_chars=_MAX), store


async def test_rows_list_the_flagged_and_rejected() -> None:
    first, second = _row("A"), _row("B")
    review, _ = _review(first, second)

    assert await review.rows() == [first, second]


async def test_edit_strips_the_text_and_stores_it_at_the_clock_time() -> None:
    row = _row()
    review, store = _review(row)

    outcome = await review.edit(row.exercise_id, "  Sit, then stand.  ")

    assert outcome == HowtoEdited()
    assert store.edits == [(row.exercise_id, "Sit, then stand.", NOW)]


async def test_edit_counts_and_stores_a_browser_line_break_as_one_character() -> None:
    # A textarea posts its line breaks as CRLF; the admin sees one character for each.
    row = _row()
    review, store = _review(row)
    typed = "x" * (_MAX - 2) + "\r\n" + "x"

    assert await review.edit(row.exercise_id, typed) == HowtoEdited()
    assert store.edits[0][1] == "x" * (_MAX - 2) + "\n" + "x"


async def test_edit_at_the_cap_is_stored() -> None:
    row = _row()
    review, store = _review(row)

    assert await review.edit(row.exercise_id, "x" * _MAX) == HowtoEdited()
    assert store.edits[0][1] == "x" * _MAX


@pytest.mark.parametrize(
    ("typed", "reason", "length"),
    [("", "empty", 0), ("   ", "empty", 0), ("x" * (_MAX + 1), "too_long", _MAX + 1)],
    ids=["empty", "whitespace", "over-the-cap"],
)
async def test_an_empty_or_over_long_edit_stores_nothing(
    typed: str, reason: Literal["empty", "too_long"], length: int
) -> None:
    row = _row()
    review, store = _review(row)

    outcome = await review.edit(row.exercise_id, typed)

    assert outcome == HowtoEditInvalid(reason=reason, length=length, limit=_MAX)
    assert store.edits == []
    assert row.exercise_id in store.rows


async def test_edit_of_a_row_no_longer_listed_is_none() -> None:
    review, _ = _review()

    assert await review.edit(uuid4(), "Sit, then stand.") is None


async def test_clear_names_the_exercise_and_a_second_clear_is_none() -> None:
    row = _row("Zz Bench")
    review, _ = _review(row)

    assert await review.clear(row.exercise_id) == HowtoCleared(exercise_name="Zz Bench")
    assert await review.clear(row.exercise_id) is None
