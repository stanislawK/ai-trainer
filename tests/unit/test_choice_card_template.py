"""The exercise card's how-to line in the template (ADR-0031 *Beginner how-to*, #590 option D,
#592): the loading line posts on load to its own route, swaps only itself, swaps nothing on a
refusal or an error, and shows the mock's skeleton while it runs; the short version carries the
sparkle, the screen-reader label and `data-simple`; the original block shows only while its row
is open with a short version."""

import re
from uuid import uuid4

from markupsafe import escape

from ai_trainer.domain.exercise_simple_texts import KeepOriginal, LoadHowto, ShortHowto
from ai_trainer.domain.exercises import ExerciseCandidate
from tests.exercise_card_support import LONG_HOW, exercise_question, render_exercise_card

_LATERAL = ExerciseCandidate(id=uuid4(), name="Dumbbell Lateral Raise", how=LONG_HOW)
_ORIGINAL = str(escape(LONG_HOW))
_SHORT = "Hold a dumbbell in each hand. Lift them out to shoulder height."


def _loader(html: str) -> str:
    [tag] = re.findall(r"<span [^>]*data-howto-load[^>]*>", html)
    return str(tag)


def _row_button(html: str) -> str:
    [tag] = re.findall(rf'<button [^>]*data-choice-option="{_LATERAL.id}"[^>]*>', html)
    return str(tag)


# AC: a failed write leaves the original and no error.
def test_howto_loader_swaps_nothing_on_error_status() -> None:
    question = exercise_question([_LATERAL])

    loader = _loader(render_exercise_card(question, howtos={str(_LATERAL.id): LoadHowto()}))

    assert 'hx-status:4xx="swap:none"' in loader
    assert 'hx-status:5xx="swap:none"' in loader


# AC: loading neither picks the row nor moves focus.
def test_howto_loader_targets_only_its_line() -> None:
    question = exercise_question([_LATERAL])

    html = render_exercise_card(question, howtos={str(_LATERAL.id): LoadHowto()})

    loader = _loader(html)
    assert f'hx-post="/clarifications/{question.id}/howto/{_LATERAL.id}"' in loader
    assert 'hx-trigger="load"' in loader
    assert 'hx-target="this"' in loader
    assert 'hx-swap="outerHTML"' in loader
    assert "autofocus" not in html
    assert "hx-focus" not in html
    # The row button still answers only on a tap, and is not the loader.
    button = _row_button(html)
    assert f'hx-post="/clarifications/{question.id}/answer"' in button
    assert "hx-trigger" not in button
    # The original shows until the request runs; the skeleton then takes its place.
    assert _ORIGINAL in html
    assert "group-[.htmx-request]/how:hidden" in html
    assert re.search(r'class="hidden group-\[\.htmx-request\]/how:flex[^"]*" role="status"', html)
    # The row keeps room for the chevron while it loads, as the mock's loading frame does.
    assert "group-has-[.htmx-request]/cand:pr-11" in button


def test_short_version_carries_the_ai_mark_and_always_the_toggle() -> None:
    question = exercise_question([_LATERAL])

    html = render_exercise_card(question, howtos={str(_LATERAL.id): ShortHowto(text=_SHORT)})

    start = html.rindex("<span", 0, html.index("data-howto data-simple"))
    line = html[start : html.index("</span>", html.index(_SHORT))]
    assert "<svg" in line
    assert "size-3" in line
    assert '<span class="sr-only">AI simplified: </span>' in line
    assert _SHORT in line
    assert "line-clamp-2" in line
    assert "data-howto-load" not in html
    # The toggle is drawn; the script shows it for every short line (#590).
    assert "data-howto-toggle" in html
    # Opened, the full original and who wrote the short one show below the row.
    original = html[html.index("data-original") :]
    assert "group-[[data-open]:has([data-simple])]/cand:flex" in html
    assert "Original instructions" in original
    assert _ORIGINAL in original
    assert "Short version simplified by AI" in original
    assert "Looks wrong" not in html


def test_keep_and_no_entry_render_the_original_as_before() -> None:
    question = exercise_question([_LATERAL])

    for howtos in ({str(_LATERAL.id): KeepOriginal()}, {}):
        html = render_exercise_card(question, howtos=howtos)
        assert _ORIGINAL in html
        assert "data-howto-load" not in html
        assert "data-original" not in html
        assert "AI simplified" not in html
