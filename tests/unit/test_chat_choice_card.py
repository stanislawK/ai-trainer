"""Asking which sport with a choice card (PRD-0004 B17, B23, F9, ADR-0015, ticket #162): a log
that stays split between two of the athlete's sports streams a card, a tap posts the option ID
and swaps the card for the chosen option above the draft, "Let me type instead" skips it, and
nothing is saved before the tap."""

import re
from datetime import date
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryClarificationRepository,
    InMemoryDraftRepository,
    InMemoryProfile,
    ScriptedConversation,
    ScriptedFastRouter,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}
# What the scripted extraction makes of "Did intervals": a ride, whichever sport it is read as.
_RIDE = CyclingExtraction.model_validate(
    {
        "occurred_on": date(2026, 10, 2),
        "date_source": "assumed_today",
        "activities": [{"distance": {"original": "62 km", "value": 62, "unit": "km"}}],
    }
)
_SPLIT = ScriptedFastRouter(
    intents=None, plausible_sports=("gym", "cycling"), single_intent="log_session"
)


class _App:
    def __init__(self, conversation: ScriptedConversation | None = None) -> None:
        app = FastAPI()
        self.drafts = InMemoryDraftRepository()
        self.clarifications = InMemoryClarificationRepository()
        self.profile = InMemoryProfile()
        self.conversation = conversation or ScriptedConversation(
            intents=[[Intent(kind="log_session", sport="gym")]], extractions=[_RIDE]
        )
        self.chat = include_chat(
            app,
            build_templates(),
            conversation=self.conversation,
            profile=self.profile,
            drafts=self.drafts,
            clarifications=self.clarifications,
            fast_router=_SPLIT,
        )
        self.user = sign_in_as(app)
        self.profile.sports[self.user.id] = ["gym", "cycling"]
        self.client = TestClient(app)

    async def asked(self, text: str = "Did intervals") -> ChatMessage:
        return await self.chat.add(
            self.user.id,
            await self.chat.conversation_of(self.user.id),
            ChatRole.USER,
            text,
            FROZEN_NOW,
        )

    async def card(self) -> tuple[ChatMessage, UUID, str]:
        """The athlete's log, the open question it left and the page's stream for it."""
        message = await self.asked()
        stream = self.client.get(f"/messages/{message.id}/reply")
        [question] = self.clarifications.all_of(self.user.id)
        return message, question.id, stream.text

    def answer(self, clarification_id: UUID | str, *option_ids: str):  # type: ignore[no-untyped-def]
        return self.client.post(
            f"/clarifications/{clarification_id}/answer",
            data={"option_ids": list(option_ids)},
            headers=_HTMX,
        )


async def test_the_stream_ends_with_a_card_offering_exactly_gym_and_cycling() -> None:
    app = _App()

    message, clarification_id, body = await app.card()

    assert f'data-choice-card="{clarification_id}"' in body
    assert body.count("data-choice-option=") == 2
    assert 'data-choice-option="gym"' in body
    assert 'data-choice-option="cycling"' in body
    assert "Which sport was that?" in body
    assert f'hx-post="/clarifications/{clarification_id}/answer"' in body
    assert "Let me type instead" in body
    assert 'data-choice-option="climbing"' not in body
    assert f'hx-target="#reply-{message.id}"' in body


async def test_an_option_posts_its_id_and_shows_its_label_and_what_sets_it_apart() -> None:
    app = _App()

    _, _, body = await app.card()

    assert """hx-vals='{"option_ids": "cycling"}'""" in body
    assert """hx-vals='{"option_ids": "gym"}'""" in body
    assert "Cycling" in body
    assert "Road, gravel, indoor" in body
    assert "Strength and conditioning" in body


async def test_asking_saves_no_draft_and_no_session() -> None:
    app = _App()

    await app.card()

    assert app.drafts.all_of(app.user.id) == []
    assert (app.conversation.routed, app.conversation.extracted) == ([], [])


async def test_a_reload_shows_the_open_card_and_does_not_ask_again() -> None:
    app = _App()
    await app.card()

    page = app.client.get("/")

    assert page.text.count("data-choice-option=") == 2
    assert "hx-sse:connect" not in page.text
    assert len(app.clarifications.all_of(app.user.id)) == 1


async def test_tapping_cycling_collapses_the_card_to_the_answer_and_shows_a_cycling_draft() -> None:
    app = _App()
    _, clarification_id, _ = await app.card()

    response = app.answer(clarification_id, "cycling")

    assert response.status_code == 200
    assert 'role="status"' in response.text
    assert "Which sport was that?" in response.text
    assert "Cycling" in response.text
    assert "data-choice-option" not in response.text
    # The athlete also has gym, so the draft's sport is a chip that offers it (#163).
    header = re.search(r"<header.*?</header>", response.text, re.S)
    assert header is not None
    assert " ".join(re.sub(r"<[^>]+>", " ", header.group()).split()).startswith(
        "Cycling , change sport Gym · draft"
    )
    [draft] = app.drafts.all_of(app.user.id)
    assert (draft.sport_id, draft.status) == ("cycling", "pending")
    assert [sport for _, sport in app.conversation.extracted] == ["cycling"]


async def test_the_answer_survives_a_reload_as_the_row_above_the_draft() -> None:
    app = _App()
    _, clarification_id, _ = await app.card()
    app.answer(clarification_id, "cycling")

    page = app.client.get("/")

    assert "data-choice-option" not in page.text
    assert f'data-choice-card="{clarification_id}"' in page.text
    row, card = page.text.index("data-choice-card="), page.text.index("data-draft-id=")
    assert row < card


async def test_an_unknown_option_id_is_rejected_and_writes_nothing() -> None:
    app = _App()
    _, clarification_id, _ = await app.card()

    for bad in ("rowing", "Cycling", "", "gym,cycling"):
        assert app.answer(clarification_id, bad).status_code == 422

    assert app.answer(clarification_id).status_code == 422
    assert app.answer(clarification_id, "gym", "cycling").status_code == 422
    assert app.drafts.all_of(app.user.id) == []
    assert app.conversation.extracted == []
    assert app.clarifications.all_of(app.user.id)[0].status == "open"


async def test_a_second_tap_is_a_409_and_makes_no_second_draft() -> None:
    app = _App()
    _, clarification_id, _ = await app.card()
    app.answer(clarification_id, "gym")

    assert app.answer(clarification_id, "cycling").status_code == 409

    assert len(app.drafts.all_of(app.user.id)) == 1


async def test_an_unknown_or_malformed_clarification_is_a_404() -> None:
    app = _App()

    assert app.answer(uuid4(), "gym").status_code == 404
    assert app.answer("not-a-uuid", "gym").status_code == 404
    abandon = app.client.post(f"/clarifications/{uuid4()}/abandon", headers=_HTMX)
    assert abandon.status_code == 404


async def test_another_users_clarification_is_a_404_and_stays_open() -> None:
    app = _App()
    _, clarification_id, _ = await app.card()
    stranger = TestClient(_strangers_app(app))

    answered = stranger.post(
        f"/clarifications/{clarification_id}/answer", data={"option_ids": ["gym"]}, headers=_HTMX
    )

    assert answered.status_code == 404
    assert app.clarifications.all_of(app.user.id)[0].status == "open"


def _strangers_app(shared: _App) -> FastAPI:
    """The same services, signed in as someone else."""
    app = FastAPI()
    include_chat(
        app,
        build_templates(),
        shared.chat,
        conversation=shared.conversation,
        profile=shared.profile,
        drafts=shared.drafts,
        clarifications=shared.clarifications,
        fast_router=_SPLIT,
    )
    sign_in_as(app)
    return app


async def test_let_me_type_instead_collapses_the_card_and_writes_nothing() -> None:
    app = _App()
    _, clarification_id, _ = await app.card()

    response = app.client.post(f"/clarifications/{clarification_id}/abandon", headers=_HTMX)

    assert response.status_code == 200
    assert "Skipped" in response.text
    assert "data-choice-option" not in response.text
    assert app.drafts.all_of(app.user.id) == []
    assert app.clarifications.all_of(app.user.id)[0].status == "abandoned"
    assert "data-choice-option" not in app.client.get("/").text
    again = app.client.post(f"/clarifications/{clarification_id}/abandon", headers=_HTMX)
    assert again.status_code == 409


async def test_a_failed_extraction_swaps_the_card_back_with_a_retry_note() -> None:
    app = _App(ScriptedConversation(extract_outcomes=[LlmCallOutcome.TIMEOUT]))
    _, clarification_id, _ = await app.card()

    response = app.answer(clarification_id, "gym")

    assert response.status_code == 200
    assert response.text.count("data-choice-option=") == 2
    assert "try again" in response.text
    assert app.drafts.all_of(app.user.id) == []
    assert app.clarifications.all_of(app.user.id)[0].status == "open"


async def test_a_stop_that_arrives_after_the_question_was_saved_shows_its_card() -> None:
    app = _App()
    message, _, _ = await app.card()

    stopped = app.client.get(f"/messages/{message.id}/stop", headers=_HTMX)

    assert stopped.text.count("data-choice-option=") == 2
