"""Asking how to read a climbing log with a choice card (PRD-0005 B17, F9, ADR-0015, ticket
#164): "Did 3 7a" from an athlete who boulders and leads streams a card offering boulders (Font
7A) or routes (French 7a), a tap swaps it for the chosen reading above the matching draft, an
option the question never offered is a 422, and nothing but the question is saved first."""

import re
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import Clarification
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.sports.climbing.extraction import (
    ClimbingExtraction,
    ClimbingQuestion,
    ExtractedClimb,
)
from ai_trainer.domain.sports.gym.extraction import GymExtraction
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
_BOULDER_OR_LEAD = ClimbingQuestion.model_validate(
    {"readings": [{"kind": "boulder", "grade": "7a"}, {"kind": "lead", "grade": "7a"}]}
)
_THREE_7A_BOULDERS = ClimbingExtraction(
    occurred_on=date(2026, 10, 2),
    date_source="assumed_today",
    activities=[ExtractedClimb(kind="boulder", count=3, grade="7a")],
)


def _session(kind: str) -> dict[str, Any]:
    return {"sport": "climbing", "activities": [{"kind": kind, "count": 1, "grade": None}]}


class _App:
    def __init__(
        self, fast: ScriptedFastRouter | None = None, sports: tuple[str, ...] = ()
    ) -> None:
        app = FastAPI()
        self.drafts = InMemoryDraftRepository()
        self.clarifications = InMemoryClarificationRepository()
        self.sessions = InMemoryTrainingSessions()
        self.profile = InMemoryProfile()
        self.conversation = ScriptedConversation(extractions=[_BOULDER_OR_LEAD, _THREE_7A_BOULDERS])
        self.chat = include_chat(
            app,
            build_templates(),
            conversation=self.conversation,
            profile=self.profile,
            drafts=self.drafts,
            clarifications=self.clarifications,
            sessions=self.sessions,
            fast_router=fast
            or ScriptedFastRouter(intents=[Intent(kind="log_session", sport="climbing")]),
        )
        self.user = sign_in_as(app)
        self.profile.sports[self.user.id] = list(sports or ("climbing",))
        self.sessions.payloads[(self.user.id, "climbing")] = [
            _session("boulder"),
            _session("lead"),
        ]
        self.client = TestClient(app)

    async def logged(self, text: str = "Did 3 7a") -> ChatMessage:
        return await self.chat.add(
            self.user.id,
            await self.chat.conversation_of(self.user.id),
            ChatRole.USER,
            text,
            FROZEN_NOW,
        )

    async def card(self) -> tuple[UUID, str]:
        message = await self.logged()
        stream = self.client.get(f"/messages/{message.id}/reply")
        [question] = self.clarifications.all_of(self.user.id)
        return question.id, stream.text

    def answer(self, clarification_id: UUID, *option_ids: str):  # type: ignore[no-untyped-def]
        return self.client.post(
            f"/clarifications/{clarification_id}/answer",
            data={"option_ids": list(option_ids)},
            headers=_HTMX,
        )


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


async def test_the_stream_ends_with_a_card_offering_boulders_font_7a_or_routes_french_7a() -> None:
    app = _App()

    clarification_id, body = await app.card()

    assert f'data-choice-card="{clarification_id}"' in body
    assert body.count("data-choice-option=") == 2
    assert """hx-vals='{"option_ids": "boulder"}'""" in body
    assert """hx-vals='{"option_ids": "lead"}'""" in body
    assert "Which did you mean?" in body
    text = _text(body)
    assert "Boulders Font 7A" in text
    assert "Routes French 7a" in text
    assert "Let me type instead" in body


async def test_asking_saves_no_draft() -> None:
    app = _App()

    await app.card()

    assert app.drafts.all_of(app.user.id) == []


async def test_a_reload_shows_the_open_card_and_does_not_ask_again() -> None:
    app = _App()
    await app.card()

    page = app.client.get("/")

    assert page.text.count("data-choice-option=") == 2
    assert "Font 7A" in _text(page.text)
    assert len(app.conversation.extracted) == 1


async def test_tapping_boulders_collapses_the_card_and_shows_a_boulder_draft() -> None:
    app = _App()
    clarification_id, _ = await app.card()

    response = app.answer(clarification_id, "boulder")

    assert response.status_code == 200
    assert "data-choice-option" not in response.text
    assert "Which did you mean?" in response.text
    assert "Boulders" in _text(response.text)
    assert "data-draft-id=" in response.text
    [draft] = app.drafts.all_of(app.user.id)
    assert (draft.sport_id, draft.status) == ("climbing", "pending")
    assert draft.draft.payload["activities"] == [
        {
            "kind": "boulder",
            "count": 3,
            "grade": {"original": "7a", "scale": "font", "french": None, "ordinal": None},
            "style": None,
            "attempts": None,
            "route": None,
        }
    ]


async def test_an_option_the_question_never_offered_is_a_422_and_writes_nothing() -> None:
    app = _App()
    clarification_id, _ = await app.card()

    for bad in ("top_rope", "Boulders", "", "climbing"):
        assert app.answer(clarification_id, bad).status_code == 422
    assert app.answer(clarification_id, "boulder", "lead").status_code == 422

    assert app.drafts.all_of(app.user.id) == []
    assert len(app.conversation.extracted) == 1
    assert app.clarifications.all_of(app.user.id)[0].status == "open"


async def test_a_sport_answered_as_climbing_shows_the_reading_card_next() -> None:
    app = _App(
        fast=ScriptedFastRouter(
            intents=None, plausible_sports=("climbing", "gym"), single_intent="log_session"
        ),
        sports=("climbing", "gym"),
    )
    message = await app.logged()
    app.client.get(f"/messages/{message.id}/reply")
    [sport_question] = app.clarifications.all_of(app.user.id)

    response = app.answer(sport_question.id, "climbing")

    assert response.status_code == 200
    reading = next(q for q in app.clarifications.all_of(app.user.id) if q.purpose == "reading")
    assert f'data-choice-card="{reading.id}"' in response.text
    assert response.text.count("data-choice-option=") == 2
    assert "Font 7A" in _text(response.text)
    assert app.drafts.all_of(app.user.id) == []


async def test_a_mixed_logs_gym_card_offers_no_climbing_chip_while_climbing_asks() -> None:
    app = _App(
        fast=ScriptedFastRouter(
            intents=[
                Intent(kind="log_session", sport="climbing"),
                Intent(kind="log_session", sport="gym"),
            ]
        ),
        sports=("climbing", "gym", "cycling"),
    )
    app.conversation.sport_extractions = {
        "climbing": _BOULDER_OR_LEAD,
        "gym": GymExtraction.model_validate(
            {
                "occurred_on": "2026-10-02",
                "date_source": "assumed_today",
                "activities": [{"name": "Squat", "sets": 5, "reps": 5}],
            }
        ),
    }
    message = await app.logged("Did 3 7a and squats 5x5")

    for body in (app.client.get(f"/messages/{message.id}/reply").text, app.client.get("/").text):
        card = body[body.index("data-draft-id=") :]
        header = re.search(r"<header.*?</header>", card, re.S)
        assert header is not None
        chips = _text(header.group())
        assert "Cycling" in chips
        assert "Climbing" not in chips


async def test_the_gym_card_shown_again_still_offers_no_climbing_chip() -> None:
    """A card re-rendered on its own (Cancel on an edit) reads the log's open questions too."""
    app = _App(
        fast=ScriptedFastRouter(
            intents=[
                Intent(kind="log_session", sport="climbing"),
                Intent(kind="log_session", sport="gym"),
            ]
        ),
        sports=("climbing", "gym", "cycling"),
    )
    app.conversation.sport_extractions = {
        "climbing": _BOULDER_OR_LEAD,
        "gym": GymExtraction.model_validate(
            {
                "occurred_on": "2026-10-02",
                "date_source": "assumed_today",
                "activities": [{"name": "Squat", "sets": 5, "reps": 5}],
            }
        ),
    }
    message = await app.logged("Did 3 7a and squats 5x5")
    app.client.get(f"/messages/{message.id}/reply")
    [gym] = app.drafts.all_of(app.user.id)

    body = app.client.get(f"/drafts/{gym.id}", headers=_HTMX).text

    header = re.search(r"<header.*?</header>", body, re.S)
    assert header is not None
    assert "Cycling" in _text(header.group())
    assert "Climbing" not in _text(header.group())


async def test_a_card_after_a_sport_answer_offers_no_chip_for_a_sport_still_asking() -> None:
    """The answer's partial reads every open question of the log, not only its own reply's."""
    app = _App(
        fast=ScriptedFastRouter(
            intents=None, plausible_sports=("climbing", "gym"), single_intent="log_session"
        ),
        sports=("climbing", "gym", "cycling"),
    )
    message = await app.logged()
    app.client.get(f"/messages/{message.id}/reply")
    [sport_question] = app.clarifications.all_of(app.user.id)
    app.conversation.extractions = [
        GymExtraction.model_validate(
            {
                "occurred_on": "2026-10-02",
                "date_source": "assumed_today",
                "activities": [{"name": "Squat", "sets": 5, "reps": 5}],
            }
        )
    ]
    # A reading question already open on this log for climbing, as a mixed log would leave.
    await app.clarifications.add(
        app.user.id,
        sport_question.conversation_id,
        message.id,
        "reading",
        Clarification(
            question="Which did you mean?",
            options=_BOULDER_OR_LEAD.options(),
            pending="log_session",
            sport="climbing",
        ),
        created_at=FROZEN_NOW,
    )

    body = app.answer(sport_question.id, "gym").text

    card = body[body.index("data-draft-id=") :]
    header = re.search(r"<header.*?</header>", card, re.S)
    assert header is not None
    assert "Cycling" in _text(header.group())
    assert "Climbing" not in _text(header.group())
