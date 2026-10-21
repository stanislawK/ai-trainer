"""Asking how to read a gym or cycling log with a choice card (PRD-0005 B3, B14, B17, F9,
ADR-0015, ticket #206): "Did 5x5 press at 60" streams a card offering the movements "press" may
be, a ride whose recent units are mixed asks km or miles, a tap shows the matching draft, nothing
but the question is saved first, and a mixed climbing and gym log drafts the climbing part while
the gym part asks."""

import re
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.sports.base import ExtractionQuestion, SessionExtraction
from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction, ExtractedClimb
from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction, CyclingQuestion
from ai_trainer.domain.sports.cycling.payload import CyclingPayload, Distance
from ai_trainer.domain.sports.gym.extraction import GymExtraction, GymQuestion
from ai_trainer.domain.sports.gym.payload import GymPayload, Load
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryClarificationRepository,
    InMemoryDraftRepository,
    InMemoryProfile,
    InMemoryTrainingSessions,
    ScriptedConversation,
    ScriptedFastRouter,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}
_PRESS = GymQuestion.model_validate(
    {"about": "movement", "exercise": "press", "readings": ["overhead press", "bench press"]}
)
# The resumed run kept the name as typed; the tap names it in code.
_PRESS_DRAFT = GymExtraction.model_validate(
    {
        "occurred_on": "2026-10-02",
        "date_source": "assumed_today",
        "activities": [
            {"name": "press", "sets": 5, "reps": 5, "load": {"original": "60", "value": 60}}
        ],
    }
)
_BENCH_UNIT = GymQuestion.model_validate(
    {"about": "unit", "exercise": "bench", "load": "60", "readings": ["lb", "kg"]}
)
_MILES_OR_KM = CyclingQuestion.model_validate({"distance": "40", "readings": ["mi", "km"]})
_RIDE_40 = CyclingExtraction.model_validate(
    {
        "occurred_on": "2026-10-02",
        "date_source": "assumed_today",
        "activities": [{"distance": {"original": "40", "value": 40}}],
    }
)
_THREE_7A_LEADS = ClimbingExtraction(
    occurred_on=date(2026, 10, 2),
    date_source="assumed_today",
    activities=[ExtractedClimb(kind="lead", count=3, grade="7a")],
)


def _ride(unit: str) -> dict[str, Any]:
    return {
        "sport": "cycling",
        "activities": [
            {"distance": {"original": f"40 {unit}", "value": 40, "unit": unit, "km": 40}}
        ],
    }


class _App:
    def __init__(
        self,
        sport: str,
        extractions: list[SessionExtraction | ExtractionQuestion | None],
        *,
        intents: list[Intent] | None = None,
    ) -> None:
        app = FastAPI()
        self.drafts = InMemoryDraftRepository()
        self.clarifications = InMemoryClarificationRepository()
        self.sessions = InMemoryTrainingSessions()
        self.profile = InMemoryProfile()
        self.conversation = ScriptedConversation(extractions=extractions)
        self.chat = include_chat(
            app,
            build_templates(),
            conversation=self.conversation,
            profile=self.profile,
            drafts=self.drafts,
            clarifications=self.clarifications,
            sessions=self.sessions,
            fast_router=ScriptedFastRouter(
                intents=intents or [Intent(kind="log_session", sport=sport)]
            ),
        )
        self.user = sign_in_as(app)
        self.profile.sports[self.user.id] = ["climbing", "gym", "cycling"]
        self.client = TestClient(app)

    async def card(self, text: str) -> tuple[UUID, str]:
        message: ChatMessage = await self.chat.add(
            self.user.id,
            await self.chat.conversation_of(self.user.id),
            ChatRole.USER,
            text,
            FROZEN_NOW,
        )
        stream = self.client.get(f"/messages/{message.id}/reply")
        [question] = self.clarifications.all_of(self.user.id)
        return question.id, stream.text

    def answer(self, clarification_id: UUID, option_id: str):  # type: ignore[no-untyped-def]
        return self.client.post(
            f"/clarifications/{clarification_id}/answer",
            data={"option_ids": [option_id]},
            headers=_HTMX,
        )


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


async def test_an_ambiguous_exercise_name_asks_which_movement() -> None:
    """AC1."""
    app = _App("gym", [_PRESS, _PRESS_DRAFT])

    clarification_id, body = await app.card("Did 5x5 press at 60")

    assert f'data-choice-card="{clarification_id}"' in body
    assert body.count("data-choice-option=") == 2
    assert """hx-vals='{"option_ids": "overhead press"}'""" in body
    assert """hx-vals='{"option_ids": "bench press"}'""" in body
    assert "Which did you mean?" in body
    text = _text(body)
    assert text.index("Overhead press") < text.index("Bench press")
    [question] = app.clarifications.all_of(app.user.id)
    assert (question.purpose, question.clarification.sport) == ("reading", "gym")


async def test_tapping_a_movement_shows_a_draft_of_that_movement() -> None:
    """AC1: the resumed run gets the pick, and the draft names the movement."""
    app = _App("gym", [_PRESS, _PRESS_DRAFT])
    clarification_id, _ = await app.card("Did 5x5 press at 60")

    response = app.answer(clarification_id, "bench press")

    assert response.status_code == 200
    assert "data-choice-option" not in response.text
    assert "Bench press" in _text(response.text)
    assert "data-draft-id=" in response.text
    assert app.conversation.extract_options[-1][1:] == (("bench press",), True)
    [draft] = app.drafts.all_of(app.user.id)
    assert (draft.sport_id, draft.status) == ("gym", "pending")
    [exercise] = GymPayload.model_validate(draft.draft.payload).activities
    assert exercise.name == "bench press"
    assert exercise.load == Load(original="60", value=60, unit="kg", kg=60)


async def test_a_load_whose_unit_is_unclear_asks_kg_or_lb_with_the_load() -> None:
    app = _App("gym", [_BENCH_UNIT])

    _, body = await app.card("Bench 3x8 @ 60")

    text = _text(body)
    assert "Pounds 60 lb" in text
    assert "Kilograms 60 kg" in text


async def test_a_ride_whose_unit_is_unclear_asks_km_or_miles() -> None:
    """AC2: the extraction reads the units of the athlete's recent rides."""
    app = _App("cycling", [_MILES_OR_KM, _RIDE_40])
    app.sessions.payloads[(app.user.id, "cycling")] = [_ride("mi"), _ride("km")]

    _, body = await app.card("Rode 40")

    assert app.conversation.extract_options[0] == (("mi", "km"), (), True)
    text = _text(body)
    assert text.index("Miles 40 mi") < text.index("Kilometres 40 km")
    assert """hx-vals='{"option_ids": "mi"}'""" in body


async def test_tapping_miles_shows_a_draft_in_miles() -> None:
    """AC2."""
    app = _App("cycling", [_MILES_OR_KM, _RIDE_40])
    clarification_id, _ = await app.card("Rode 40")

    response = app.answer(clarification_id, "mi")

    assert response.status_code == 200
    assert "Miles" in _text(response.text)
    assert "64.4" in _text(response.text)
    [draft] = app.drafts.all_of(app.user.id)
    [ride] = CyclingPayload.model_validate(draft.draft.payload).activities
    assert ride.distance == Distance(original="40", value=40, unit="mi", km=64.37)


async def test_asking_saves_nothing_but_the_question() -> None:
    """AC4, with fakes; `test_reading_question_repository.py` checks the tables."""
    for sport, question in (("gym", _PRESS), ("cycling", _MILES_OR_KM)):
        app = _App(sport, [question])

        await app.card("Did 5x5 press at 60")

        assert app.drafts.all_of(app.user.id) == []
        assert [q.status for q in app.clarifications.all_of(app.user.id)] == ["open"]


async def test_an_option_the_question_never_offered_is_a_422_and_writes_nothing() -> None:
    app = _App("gym", [_PRESS, _PRESS_DRAFT])
    clarification_id, _ = await app.card("Did 5x5 press at 60")

    for bad in ("leg press", "Overhead press", "kg", "gym"):
        assert app.answer(clarification_id, bad).status_code == 422

    assert app.drafts.all_of(app.user.id) == []
    assert len(app.conversation.extracted) == 1


async def test_a_mixed_log_drafts_the_climbing_part_and_asks_about_the_gym_part() -> None:
    """AC5."""
    app = _App(
        "gym",
        [_THREE_7A_LEADS],
        intents=[
            Intent(kind="log_session", sport="climbing"),
            Intent(kind="log_session", sport="gym"),
        ],
    )
    app.conversation.sport_extractions = {"climbing": _THREE_7A_LEADS, "gym": _PRESS}

    clarification_id, body = await app.card("Did 3 7a on lead, then 5x5 press at 60")

    [climbing] = app.drafts.all_of(app.user.id)
    assert (climbing.sport_id, climbing.status) == ("climbing", "pending")
    assert body.index("data-draft-id=") < body.index(f'data-choice-card="{clarification_id}"')
    assert "Overhead press" in _text(body)
    # No guess past the open question: the climbing card offers no switch to gym (ADR-0015).
    header = re.search(r"<header.*?</header>", body[body.index("data-draft-id=") :], re.S)
    assert header is not None
    assert "Gym" not in _text(header.group())
    assert "Cycling" in _text(header.group())

    app.conversation.sport_extractions["gym"] = _PRESS_DRAFT
    response = app.answer(clarification_id, "overhead press")

    assert response.status_code == 200
    drafts = app.drafts.all_of(app.user.id)
    assert sorted(draft.sport_id for draft in drafts) == ["climbing", "gym"]
    [gym] = [draft for draft in drafts if draft.sport_id == "gym"]
    assert GymPayload.model_validate(gym.draft.payload).activities[0].name == "overhead press"
