"""The band-order card (PRD-0010 B39, ADR-0015, ticket #431): rows of places lightest first, each
with the order it would give, "Skip for now" in place of "Let me type instead", an answered row
that shows the new order, and a skipped row that is dashed. A tap posts the option's ID."""

from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.clarification import BAND_PURPOSE, StoredClarification
from ai_trainer.domain.sports.gym.bands import band_question
from ai_trainer.web.clarifications import ChoiceCard
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryClarificationRepository,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}


class _Scale:
    """The athlete's bands, lightest first, with the ranks `place` was asked to give."""

    def __init__(self, ranked: list[str], waiting: list[str] | None = None) -> None:
        self.labels = ranked
        self.waiting = waiting or []
        self.placed: list[tuple[str, int]] = []

    async def ranked(self, user_id: UUID) -> list[str]:
        return list(self.labels)

    async def place(self, user_id: UUID, label: str, rank: int) -> None:
        self.placed.append((label, rank))
        self.labels.insert(rank - 1, label)

    async def next_unplaced(self, user_id: UUID) -> str | None:
        return self.waiting.pop(0) if self.waiting else None


class _App:
    def __init__(self, scale: _Scale) -> None:
        app = FastAPI()
        self.clarifications = InMemoryClarificationRepository()
        self.scale = scale
        include_chat(
            app, build_templates(), clarifications=self.clarifications, band_orders={"gym": scale}
        )
        self.user = sign_in_as(app)
        self.client = TestClient(app)

    async def ask(self, label: str, ranked: list[str], *, max_options: int = 5) -> UUID:
        asked = await self.clarifications.add(
            self.user.id,
            uuid4(),
            uuid4(),
            BAND_PURPOSE,
            band_question(label, ranked, max_options=max_options),
            created_at=FROZEN_NOW,
        )
        return asked.id

    def only(self) -> StoredClarification:
        return self.clarifications.all_of(self.user.id)[0]

    def post(self, question_id: UUID, action: str, *option_ids: str):  # type: ignore[no-untyped-def]
        return self.client.post(
            f"/clarifications/{question_id}/{action}",
            data={"option_ids": list(option_ids)},
            headers=_HTMX,
        )


def _flat(html: str) -> str:
    return " ".join(html.split())


async def test_a_tap_answers_with_the_new_order_and_a_second_band_asks_next() -> None:
    app = _App(_Scale(["red", "green"], waiting=["blue"]))
    question_id = await app.ask("black", ["red", "green"])

    response = app.post(question_id, "answer", "place:2")

    assert response.status_code == 200
    html = _flat(response.text)
    assert "Band order" in html
    assert "red &lt; black &lt; green" in html
    assert app.scale.placed == [("black", 2)]
    assert "Where does your blue band go among your bands?" in html
    assert html.index("Band order") < html.index("blue")


async def test_a_card_with_the_rows_in_order_and_skip_in_place_of_typing() -> None:
    app = _App(_Scale(["red"]))
    question_id = await app.ask("black", ["red"])
    stored = app.only()

    html = _flat(
        build_templates()
        .env.from_string(
            '{% from "components/choice_card.html" import band_card %}{{ band_card(card) }}'
        )
        .render(card=ChoiceCard(stored))
    )

    assert html.count("data-choice-option=") == 2
    assert html.index("Lighter than red") < html.index("Heavier than red")
    assert "black &lt; red" in html
    assert "red &lt; black" in html
    assert "Skip for now" in html
    assert "Let me type instead" not in html
    assert f'hx-post="/clarifications/{question_id}/abandon"' in html


async def test_skipping_shows_the_dashed_row_and_keeps_the_band_by_its_label() -> None:
    app = _App(_Scale(["red"]))
    question_id = await app.ask("black", ["red"])

    response = app.post(question_id, "abandon")

    assert response.status_code == 200
    html = _flat(response.text)
    assert "border-dashed" in html
    assert "black stays by its name" in html
    assert app.only().status == "abandoned"
    assert app.scale.placed == []


async def test_more_bands_than_the_cap_narrow_the_card_in_place() -> None:
    ranked = ["b1", "b2", "b3", "b4", "b5", "b6"]
    app = _App(_Scale(ranked))
    question_id = await app.ask("black", ranked)

    response = app.post(question_id, "answer", "narrow:5-7")

    assert response.status_code == 200
    html = _flat(response.text)
    assert html.count("data-choice-option=") == 3
    assert "Heavier than b6" in html
    assert app.only().status == "open"
    assert app.scale.placed == []


async def test_a_place_never_offered_is_a_422_and_another_athletes_card_a_404() -> None:
    app = _App(_Scale(["red"]))
    question_id = await app.ask("black", ["red"])

    assert app.post(question_id, "answer", "place:9").status_code == 422
    assert app.post(uuid4(), "answer", "place:1").status_code == 404
    assert app.post(question_id, "answer", "place:1", "place:2").status_code == 422
    assert app.only().status == "open"
    assert app.scale.placed == []
