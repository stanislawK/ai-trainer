"""The exercise card's how-to line in the template (ADR-0031 *Beginner how-to*, #590 option D,
#592): the loading line posts on load to its own route, swaps only itself, swaps nothing on a
refusal or an error, and shows the mock's skeleton while it runs; the short version carries the
sparkle, the screen-reader label and `data-simple`; the original block shows only while its row
is open with a short version. "Looks wrong" (#593) sits in that block, outside the row button, and
a just-flagged candidate shows the original with a status line."""

import re
from uuid import uuid4

from markupsafe import escape

from ai_trainer.domain.exercise_simple_texts import KeepOriginal, LoadHowto, ShortHowto
from ai_trainer.domain.exercises import ExerciseCandidate
from ai_trainer.web.templating import build_templates
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
    # The row keeps room for the chevron while its how-to loads, as the mock's loading frame
    # does; a picture slot loading in the same row (#643) leaves the padding alone.
    assert "group-has-[.htmx-request:not([data-picture-slot])]/cand:pr-11" in button
    assert "group-has-[.htmx-request]/cand:pr-11" not in button


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


def test_keep_and_no_entry_render_the_original_as_before() -> None:
    question = exercise_question([_LATERAL])

    for howtos in ({str(_LATERAL.id): KeepOriginal()}, {}):
        html = render_exercise_card(question, howtos=howtos)
        assert _ORIGINAL in html
        assert "data-howto-load" not in html
        assert "data-original" not in html
        assert "AI simplified" not in html


def _flag_control(html: str) -> str:
    [tag] = re.findall(r"<button [^>]*data-howto-flag[^>]*>", html)
    return str(tag)


# AC (#593): "Looks wrong" never answers the question and asks no reason.
def test_short_line_offers_looks_wrong_outside_the_row_button() -> None:
    question = exercise_question([_LATERAL])

    html = render_exercise_card(question, howtos={str(_LATERAL.id): ShortHowto(text=_SHORT)})

    control = _flag_control(html)
    assert f'hx-post="/clarifications/{question.id}/howto/{_LATERAL.id}/flag"' in control
    assert 'hx-target="closest [data-candidate]"' in control
    assert 'hx-swap="outerHTML"' in control
    assert 'hx-status:4xx="swap:none"' in control
    assert 'type="button"' in control
    assert "hx-vals" not in control
    assert "Looks wrong" in html[html.index("data-howto-flag") :]
    # It sits in the open row's original block, after the row button has closed.
    row_start = html.index(f'data-choice-option="{_LATERAL.id}"')
    row_end = html.index("</button>", row_start)
    assert html.index("data-original") > row_end
    assert html.index("data-howto-flag") > html.index("data-original")
    # One tap, no reason: no field, select or textarea anywhere on the card.
    for tag in ("<input", "<textarea", "<select"):
        assert tag not in html


def test_flagged_candidate_shows_the_status_line() -> None:
    question = exercise_question([_LATERAL])
    template = build_templates().env.from_string(
        '{% from "components/choice_card.html" import exercise_candidate %}'
        "{{ exercise_candidate(question, option, 1, none, howto, flagged=true) }}"
    )

    html = template.render(
        question=question, option=question.clarification.options[0], howto=KeepOriginal()
    )

    assert html.lstrip().startswith("<div")
    assert f'data-candidate="{_LATERAL.id}"' in html
    assert _ORIGINAL in html
    status = html[html.rindex("<p", 0, html.index('role="status"')) :]
    assert "Thanks, the short version is hidden. Showing the original." in status
    assert "data-simple" not in html
    assert "AI simplified" not in html
    assert "data-original" not in html
    assert "data-howto-flag" not in html
    # #580's chevron is drawn, so the script shows it when the original overflows.
    assert "data-howto-toggle" in html
    # Under the row, at the text column of a text-only row.
    assert html.index('role="status"') > html.index("</button>")
    assert "pl-4" in status
