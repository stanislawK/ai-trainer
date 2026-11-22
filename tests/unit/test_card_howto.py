"""What the exercise card shows in a candidate's how-to line (ADR-0031 *Beginner how-to*, #592):
a current or edited `shown` text straight away, a `rejected` or flagged row the original with no
request, and no row, a stale one or a text-less one the original with a request that writes it."""

from datetime import UTC, datetime

from ai_trainer.domain.exercise_simple_texts import (
    KeepOriginal,
    LoadHowto,
    ShortHowto,
    SimpleTextStatus,
    StoredSimpleText,
    card_howto,
    source_sha256,
)

_SHA = source_sha256("Lift the dumbbells out to shoulder height.")
_STALE = "0" * 64
_EDITED = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
_TEXT = "Lift them out to shoulder height."


def _stored(
    *,
    status: SimpleTextStatus = SimpleTextStatus.SHOWN,
    sha: str = _SHA,
    edited: bool = False,
    has_text: bool = True,
) -> StoredSimpleText:
    return StoredSimpleText(
        source_sha256=sha,
        status=status,
        edited_at=_EDITED if edited else None,
        has_text=has_text,
        check_failures=0 if has_text else 1,
    )


# AC: a custom exercise or a `rejected` row makes no request.
def test_custom_rejected_and_flagged_rows_keep_the_original() -> None:
    rejected = _stored(status=SimpleTextStatus.REJECTED)
    flagged = _stored(status=SimpleTextStatus.FLAGGED)

    assert card_howto(rejected, _SHA, _TEXT) == KeepOriginal()
    assert card_howto(flagged, _SHA, _TEXT) == KeepOriginal()
    # An edited flagged row stays hidden: only an admin's clear or a new status shows it.
    assert card_howto(_stored(status=SimpleTextStatus.FLAGGED, edited=True), _SHA, _TEXT) == (
        KeepOriginal()
    )


def test_a_current_shown_text_is_shown_with_no_request() -> None:
    assert card_howto(_stored(), _SHA, _TEXT) == ShortHowto(text=_TEXT)


def test_an_edited_text_is_shown_even_when_stale_or_once_rejected() -> None:
    assert card_howto(_stored(sha=_STALE, edited=True), _SHA, _TEXT) == ShortHowto(text=_TEXT)
    edited_rejected = _stored(status=SimpleTextStatus.REJECTED, edited=True)
    assert card_howto(edited_rejected, _SHA, _TEXT) == ShortHowto(text=_TEXT)


def test_no_row_a_stale_row_or_a_text_less_row_loads() -> None:
    assert card_howto(None, _SHA, None) == LoadHowto()
    assert card_howto(_stored(sha=_STALE), _SHA, _TEXT) == LoadHowto()
    # A stale flag hides old text: new instructions are written again (ADR-0031 *Stale text*).
    assert card_howto(_stored(status=SimpleTextStatus.FLAGGED, sha=_STALE), _SHA, _TEXT) == (
        LoadHowto()
    )
    assert card_howto(_stored(has_text=False), _SHA, None) == LoadHowto()


def test_an_edited_row_with_no_text_keeps_the_original() -> None:
    assert card_howto(_stored(edited=True, has_text=False), _SHA, None) == KeepOriginal()
