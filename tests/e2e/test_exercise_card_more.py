"""The exercise card's More in a real browser (F23, ADR-0015, ADR-0031, #581): the one place a tap
could answer a question by accident. The card is rendered through the app's Jinja environment
with fixture candidates and served with the built `app.css`, htmx and `exercise_card.js` through
`page.route`, so it needs no seeded chat (app.css comes from the running app when the checkout
has none built, as in CI).
More expands and Less collapses a long how-to without posting an answer; a tap on the row still
posts one; a short how-to keeps its toggle hidden and its row at full width; the keyboard works."""

from pathlib import Path
from uuid import uuid4

import pytest
from playwright.sync_api import Locator, Page, Request, Route, expect

from ai_trainer.domain.exercises import ExerciseCandidate
from tests.e2e.conftest import BASE_URL
from tests.exercise_card_support import (
    LONG_HOW,
    SHORT_HOW,
    WGER_PICTURE,
    exercise_question,
    render_exercise_card,
)

_STATIC = Path(__file__).resolve().parents[2] / "src" / "ai_trainer" / "web" / "static"
_ORIGIN = "http://card.test"
_LATERAL = ExerciseCandidate(id=uuid4(), name="Dumbbell Lateral Raise", how=LONG_HOW)
_FRONT = ExerciseCandidate(id=uuid4(), name="Dumbbell Front Raise", how=LONG_HOW)
_BAND = ExerciseCandidate(id=uuid4(), name="Band Lateral Raise", how=SHORT_HOW)
_CUSTOM = ExerciseCandidate(id=uuid4(), name="My Raise")
_TYPES = {
    ".css": "text/css",
    ".js": "text/javascript",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


def _document() -> str:
    question = exercise_question([_LATERAL, _FRONT, _BAND, _CUSTOM])
    card = render_exercise_card(question, {str(_FRONT.id): WGER_PICTURE})
    return (
        '<!doctype html><html data-theme="trainer-dark"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<link rel="stylesheet" href="/static/css/app.css">'
        '<script src="/static/vendor/htmx-4.0.0.min.js"></script>'
        '<script src="/static/js/exercise_card.js" defer></script></head>'
        '<body class="wallpaper"><main class="max-w-[720px] mx-auto p-4">'
        f"{card}</main></body></html>"
    )


class _Card:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.answers: list[str] = []

    def candidate(self, exercise: ExerciseCandidate) -> Locator:
        return self.page.locator(f'[data-candidate="{exercise.id}"]')

    def toggle(self, exercise: ExerciseCandidate) -> Locator:
        return self.candidate(exercise).locator("[data-howto-toggle]")

    def row(self, exercise: ExerciseCandidate) -> Locator:
        return self.page.locator(f'[data-choice-option="{exercise.id}"]')


@pytest.fixture
def card(page: Page) -> _Card:
    """The card at 390 px, every request answered from disk or recorded."""
    shown = _Card(page)
    document = _document()

    def serve(route: Route, request: Request) -> None:
        path = request.url.removeprefix(_ORIGIN)
        if path.startswith("/clarifications/"):
            shown.answers.append(path)
            route.fulfill(status=200, body="<div></div>", content_type="text/html")
        elif path.startswith("/static/"):
            file = _STATIC / path.removeprefix("/static/")
            if file.is_file():
                route.fulfill(path=file, content_type=_TYPES.get(file.suffix, "text/plain"))
            else:
                # CI builds app.css only in the compose image: take it from the running app.
                route.fulfill(response=route.fetch(url=f"{BASE_URL}{path}"))
        else:
            route.fulfill(body=document, content_type="text/html")

    page.route(f"{_ORIGIN}/**", serve)
    page.goto(f"{_ORIGIN}/")
    expect(shown.toggle(_LATERAL)).to_be_visible()
    return shown


def _lines(text: Locator) -> float:
    height: float = text.evaluate("e => e.getBoundingClientRect().height")
    return height / 18


def test_more_expands_and_less_collapses(card: _Card) -> None:
    text = card.candidate(_LATERAL).locator("[data-howto]")
    toggle = card.toggle(_LATERAL)
    assert _lines(text) == pytest.approx(2, abs=0.1)

    toggle.click()

    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(toggle).to_have_attribute("aria-label", "Show less of Dumbbell Lateral Raise")
    assert _lines(text) > 2.5

    toggle.click()

    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(toggle).to_have_attribute("aria-label", "Show more of Dumbbell Lateral Raise")
    assert _lines(text) == pytest.approx(2, abs=0.1)
    # The picture row has its toggle too, beside its credit line.
    expect(card.toggle(_FRONT)).to_be_visible()


def test_more_does_not_post_an_answer_and_row_tap_does(card: _Card) -> None:
    card.toggle(_LATERAL).click()
    card.toggle(_FRONT).click()
    card.toggle(_FRONT).click()
    card.page.wait_for_timeout(300)

    assert card.answers == []

    with card.page.expect_request(lambda request: request.url.endswith("/answer")):
        card.row(_LATERAL).click()

    assert len(card.answers) == 1
    assert card.answers[0].endswith("/answer")


def test_short_text_keeps_toggle_hidden_and_full_width(card: _Card) -> None:
    expect(card.toggle(_BAND)).to_be_hidden()
    padding = card.row(_BAND).evaluate("e => getComputedStyle(e).paddingRight")
    padded = card.row(_LATERAL).evaluate("e => getComputedStyle(e).paddingRight")
    assert padded == "44px"
    assert padding != "44px"
    # A custom exercise with no text has no toggle at all.
    assert card.candidate(_CUSTOM).locator("[data-howto-toggle]").count() == 0


def test_toggle_by_keyboard_flips_aria_expanded(card: _Card) -> None:
    toggle = card.toggle(_LATERAL)
    card.row(_LATERAL).focus()

    card.page.keyboard.press("Tab")

    expect(toggle).to_be_focused()
    card.page.keyboard.press("Enter")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    card.page.keyboard.press("Space")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    card.page.wait_for_timeout(300)
    assert card.answers == []
