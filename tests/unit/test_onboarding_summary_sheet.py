"""The onboarding summary's glass sheet (PRD 0008 F21, ADR-0019, ADR-0028, ticket #257): while
the answers are read, the summary is the `lg-sheet` slab with a group of field wells per
section and cycling status lines; when the summary lands the sheet settles before the card
replaces it, and under `prefers-reduced-motion` nothing drifts or travels."""

import re
from collections.abc import Iterator

import pytest

from ai_trainer.web.templating import STATIC_DIR
from tests.unit.test_ai_onboarding_more import _climbed, _done
from tests.unit.test_ai_onboarding_route import HX, Athlete, _answered, _athlete, _summary
from tests.unit.test_theme import _utility_block

SHEET_UTILITIES = (
    "lg-sheet",
    "lg-sheet-floor",
    "lg-sheet-slab",
    "lg-sheet-face",
    "lg-sheet-light",
    "lg-sheet-ox",
    "lg-sheet-oy",
    "lg-sheet-photon",
    "lg-sheet-caustic",
    "lg-sheet-spec",
    "lg-sheet-content",
    "lg-sheet-well",
)


@pytest.fixture
def athlete() -> Iterator[Athlete]:
    yield from _athlete()


def _well_groups(html: str) -> list[str]:
    """The section titles that each head a group of three wells, in page order."""
    return re.findall(
        r'([^<>]+)</div>\s*<div class="grid grid-cols-2 gap-2" aria-hidden="true">\s*'
        r'(?:<span class="lg-sheet-well[^"]*"></span>\s*){3}</div>',
        html,
    )


def _glass_css() -> str:
    return (STATIC_DIR / "css" / "glass.css").read_text()


# --- AC1: one group of wells per section ---------------------------------------------------


async def test_the_pending_summary_is_the_sheet_with_wells_for_about_you_and_the_sport(
    athlete: Athlete,
) -> None:
    await _answered(athlete)

    html = athlete.client.get("/onboarding/summary").text

    assert 'class="lg-sheet"' in html
    for layer in ("floor", "slab", "face", "light", "photon", "caustic", "spec", "content"):
        assert f"lg-sheet-{layer}" in html, layer
    assert _well_groups(html) == ["About you", "Climbing"]
    assert html.count("lg-sheet-well") == 6
    assert "skeleton h-11" not in html


async def test_a_second_sport_adds_its_own_group_of_wells(athlete: Athlete) -> None:
    await _climbed(athlete)
    athlete.client.post("/onboarding/more", data={"sport": "gym"}, headers=HX)
    await _done(athlete.answers, athlete.user.id, "gym")

    html = athlete.client.get("/onboarding/summary").text

    assert _well_groups(html) == ["About you", "Climbing", "Gym"]
    assert html.count("lg-sheet-well") == 9


async def test_the_sheet_cycles_the_mocks_status_lines_one_per_sport(athlete: Athlete) -> None:
    await _climbed(athlete)
    athlete.client.post("/onboarding/more", data={"sport": "gym"}, headers=HX)
    await _done(athlete.answers, athlete.user.id, "gym")

    html = athlete.client.get("/onboarding/summary").text
    lines = re.findall(r"<span[^>]*data-sheet-line[^>]*>([^<]+)</span>", html)

    assert lines == [
        "Reading your answers…",
        "Picking out the numbers…",
        "Setting up Climbing…",
        "Setting up Gym…",
        "Checking for gaps…",
    ]
    assert '<script src="/static/js/onboarding_sheet.js" defer></script>' in html
    # The sheet's own label is what a screen reader hears; the cycling lines are decoration.
    assert re.search(r'<div class="lg-sheet"[^>]*role="status"[^>]*aria-label="Building', html)
    assert re.search(r'<span[^>]*aria-hidden="true"[^>]*>\s*<span[^>]*data-sheet-line', html)


async def test_the_sheet_partial_carries_its_script_for_an_htmx_swap(athlete: Athlete) -> None:
    await _answered(athlete)

    response = athlete.client.get("/onboarding/summary", headers=HX)

    assert 'class="lg-sheet"' in response.text
    assert "/static/js/onboarding_sheet.js" in response.text


def test_the_sheet_script_exists_and_only_toggles_a_phase() -> None:
    script = (STATIC_DIR / "js" / "onboarding_sheet.js").read_text()

    assert "data-sheet-line" in script
    assert "dataset.phase" in script
    # The words live in the template (G5).
    assert "Reading your answers" not in script


# --- AC2: the sheet settles before the card appears ---------------------------------------


async def test_the_summary_waits_for_the_sheet_to_settle_before_it_swaps(
    athlete: Athlete,
) -> None:
    await _answered(athlete)

    event = _summary(athlete)

    assert 'hx-swap="innerHTML swap:650ms"' in event
    assert 'hx-target="#onboarding-summary"' in event


def test_the_sheet_settles_while_htmx_is_swapping_or_when_marked_settled() -> None:
    block = _utility_block(_glass_css(), "lg-sheet")

    assert "&.is-settled, .htmx-swapping &" in block
    settled = block.partition("&.is-settled, .htmx-swapping &")[2]
    assert "transform: none" in settled
    assert "animation: none" in settled
    for layer in ("light", "caustic", "floor"):
        assert f".lg-sheet-{layer}" in settled, layer
    assert "opacity: 0" in settled


# --- AC3: reduced motion -------------------------------------------------------------------


def test_glass_css_harvests_every_sheet_utility_and_its_keyframes() -> None:
    css = _glass_css()

    for utility in SHEET_UTILITIES:
        assert f"@utility {utility} {{" in css, utility
    for keyframes in ("lg-sheet-float", "lg-sheet-breathe", "lg-sheet-x", "lg-sheet-y"):
        assert f"@keyframes {keyframes} {{" in css, keyframes


def test_reduced_motion_stops_the_tilt_drift_and_the_light_travel() -> None:
    css = _glass_css()

    for utility in ("lg-sheet", "lg-sheet-ox", "lg-sheet-oy", "lg-sheet-caustic", "lg-sheet-floor"):
        block = _utility_block(css, utility)
        assert "animation:" in block, utility
        reduced = block.partition("@media (prefers-reduced-motion: reduce)")[2]
        assert "animation: none" in reduced, utility


def test_reduced_motion_settles_the_sheet_without_rotating_it_flat() -> None:
    """Skeptic review, AC3: the settle's .6 s transform transition still rotated the slab from
    9° to flat once under reduced motion."""
    block = _utility_block(_glass_css(), "lg-sheet")

    assert "transition: transform" in block
    reduced = block.partition("@media (prefers-reduced-motion: reduce)")[2]
    assert "transition: none" in reduced


def test_the_sheet_face_turns_solid_under_reduced_transparency() -> None:
    block = _utility_block(_glass_css(), "lg-sheet-face")

    query = "@media (prefers-reduced-transparency: reduce), (prefers-contrast: more)"
    reduced = block.partition(query)[2]
    assert "backdrop-filter: none" in reduced
