"""The exercise card's More (F23, ADR-0031, #581; the Exercise Card mock, #580 option 2c): each
candidate with a how-to renders its whole text clamped at two lines, beside a hidden chevron toggle
that `static/js/exercise_card.js` shows only when the text overflows. The toggle is a sibling of the
row button with no htmx attribute, so it never answers the question (ADR-0015). A candidate with no
text has no toggle at all."""

import re
from uuid import uuid4

from markupsafe import escape

from ai_trainer.domain.exercises import ExerciseCandidate
from tests.exercise_card_support import (
    LONG_HOW,
    SHORT_HOW,
    WGER_PICTURE,
    exercise_question,
    render_exercise_card,
)

_LATERAL = ExerciseCandidate(id=uuid4(), name="Dumbbell Lateral Raise", how=LONG_HOW)
_BAND = ExerciseCandidate(id=uuid4(), name="Band Lateral Raise", how=SHORT_HOW)
_CUSTOM = ExerciseCandidate(id=uuid4(), name="My Raise")


def _block(html: str, candidate: ExerciseCandidate) -> str:
    """The candidate's wrapper, up to the next candidate or "Keep my name"."""
    marker = html.index(f'data-candidate="{candidate.id}"')
    following = [
        html.rindex("<", 0, index)
        for other in ("data-candidate=", 'data-choice-option="keep"')
        if (index := html.find(other, marker + 1)) != -1
    ]
    return html[html.rindex("<", 0, marker) : min(following)]


def _toggle(block: str) -> str:
    [toggle] = re.findall(r"<button[^>]*data-howto-toggle[^>]*>", block)
    return str(toggle)


def _row(block: str, candidate: ExerciseCandidate) -> str:
    [row] = re.findall(
        rf'<button[^>]*data-choice-option="{candidate.id}"[^>]*>.*?</button>', block, re.S
    )
    return str(row)


def test_card_renders_full_text_clamped_with_collapsed_toggle() -> None:
    html = render_exercise_card(exercise_question([_LATERAL, _BAND]))

    block = _block(html, _LATERAL)
    [text] = re.findall(r"<span[^>]*data-howto[^>]*>[^<]*</span>", block)
    assert str(escape(LONG_HOW)) in text
    # Two lines on the phone and the desktop alike (#580); open lifts the clamp.
    assert "line-clamp-2" in text
    assert "line-clamp-1" not in block
    assert "group-data-open/cand:line-clamp-none" in text
    toggle = _toggle(block)
    assert 'aria-expanded="false"' in toggle
    # Hidden until the script measures an overflow; then the row reserves 44 px on its right.
    assert re.search(r'class="[^"]*\bhidden\b[^"]*\bgroup-data-more/cand:grid\b', toggle)
    assert "group-data-more/cand:pr-11" in _row(block, _LATERAL)


def test_toggle_is_outside_the_row_button_with_no_hx_attributes() -> None:
    html = render_exercise_card(exercise_question([_LATERAL, _BAND]))

    block = _block(html, _LATERAL)
    row = _row(block, _LATERAL)
    toggle = _toggle(block)
    assert "data-howto-toggle" not in row
    assert block.index(row) + len(row) <= block.index(toggle)
    assert 'type="button"' in toggle
    assert "hx-" not in toggle
    assert "data-choice-option" not in toggle
    # A text-only candidate loses its own right-hand chevron, which the toggle would double.
    assert "chevron-right" not in row


def test_row_without_text_has_no_toggle() -> None:
    html = render_exercise_card(exercise_question([_LATERAL, _CUSTOM]))

    block = _block(html, _CUSTOM)
    assert "data-howto-toggle" not in block
    assert "data-howto" not in block
    assert "My Raise" in block


def test_toggle_has_aria_expanded_and_label() -> None:
    html = render_exercise_card(exercise_question([_LATERAL]))

    block = _block(html, _LATERAL)
    toggle = _toggle(block)
    [text_id] = re.findall(r'<span id="([^"]+)"[^>]*data-howto', block)
    assert f'aria-controls="{text_id}"' in toggle
    assert 'aria-label="Show more of Dumbbell Lateral Raise"' in toggle
    assert 'data-label-more="Show more of Dumbbell Lateral Raise"' in toggle
    assert 'data-label-less="Show less of Dumbbell Lateral Raise"' in toggle
    # The invisible 52 px tap target around the 28 px button.
    assert '<span class="absolute -inset-3" aria-hidden="true"></span>' in block
    assert "group-data-open/cand:rotate-180" in block


def test_media_row_has_the_toggle_and_the_quiet_credit_line() -> None:
    html = render_exercise_card(
        exercise_question([_LATERAL, _BAND]), {str(_LATERAL.id): WGER_PICTURE}
    )

    block = _block(html, _LATERAL)
    assert "<img" in block
    assert "data-howto-toggle" in block
    # The credit is one quiet line aligned to the text column, its links underlined text.
    credit = block[block.index('data-credit="wger"') - 200 :]
    assert "pl-[76px] sm:pl-[92px]" in credit
    assert "lg-chip" not in block
    links = re.findall(r"<a [^>]*>", credit)
    assert len(links) == 2
    assert all("underline" in link for link in links)
    assert "K. Brandt" in credit
    assert ">CC BY-SA 4.0</a>" in credit


def test_each_text_id_is_unique_on_the_card() -> None:
    html = render_exercise_card(exercise_question([_LATERAL, _BAND]))

    ids = re.findall(r'<span id="([^"]+)"[^>]*data-howto', html)
    assert len(ids) == 2
    assert len(set(ids)) == 2
