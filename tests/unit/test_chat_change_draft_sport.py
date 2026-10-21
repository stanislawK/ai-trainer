"""The draft card's sport chip (PRD-0005 B23, F2, ticket #163): it offers the athlete's other
sports that can be logged, a pick re-runs that sport's extraction on the same log and swaps in
the new card, and a failed one keeps the card with a note to pick again. The chip's markup was
owner-chosen at #163's plan gate, since the Chat mock has no frame for it."""

import re
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.training import SessionDraft, StoredDraft
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CsrfMiddleware
from ai_trainer.web.icons import render_icon
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    UNLOGGABLE_SPORT,
    InMemoryDraftRepository,
    InMemoryProfile,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}
_INTERVALS = "Did intervals 45 min"
_TODAY = date(2026, 10, 2)
_GYM = GymExtraction.model_validate(
    {"occurred_on": _TODAY, "date_source": "assumed_today", "activities": [{"name": "Intervals"}]}
)
_RIDE = CyclingExtraction.model_validate(
    {"occurred_on": _TODAY, "date_source": "assumed_today", "activities": [{"moving_time_min": 45}]}
)


class _App:
    def __init__(self, *, sports: tuple[str, ...] = ("gym", "cycling")) -> None:
        app = FastAPI()
        self.drafts = InMemoryDraftRepository()
        self.profile = InMemoryProfile()
        self.conversation = ScriptedConversation(
            intents=[[Intent(kind="log_session", sport="gym")]], extractions=[_GYM, _RIDE]
        )
        self.chat = include_chat(
            app,
            build_templates(),
            conversation=self.conversation,
            drafts=self.drafts,
            profile=self.profile,
        )
        self.user_id = sign_in_as(app).id
        self.profile.sports[self.user_id] = list(sports)
        self.app = app
        self.client = TestClient(app)

    async def log(self, text: str = _INTERVALS) -> ChatMessage:
        return await self.chat.add(
            self.user_id,
            await self.chat.conversation_of(self.user_id),
            ChatRole.USER,
            text,
            FROZEN_NOW,
        )

    async def gym_draft(self) -> StoredDraft:
        """The athlete's log, its gym extraction reply and the pending gym draft under it."""
        log = await self.log()
        await self.chat.add(
            self.user_id,
            log.conversation_id,
            ChatRole.ASSISTANT,
            "Here's the session.",
            FROZEN_NOW + timedelta(microseconds=1),
            sports=("gym",),
            template_id="gym.extract",
            template_version=1,
        )
        return await self.drafts.add(
            self.user_id,
            log.conversation_id,
            log.id,
            "gym",
            SessionDraft(
                occurred_on=_TODAY,
                date_source="assumed_today",
                timezone="Europe/Warsaw",
                payload=_GYM.to_payload().model_dump(mode="json"),
            ),
            payload_version=1,
            created_at=FROZEN_NOW,
        )

    def switch(self, draft_id: UUID | str, sport_id: str):  # type: ignore[no-untyped-def]
        return self.client.post(
            f"/drafts/{draft_id}/sport", data={"sport_id": sport_id}, headers=_HTMX
        )


def _card(html: str) -> str:
    card: str
    (card,) = re.findall(r'<article[^>]*aria-label="Draft session".*?</article>', html, re.S)
    return card


def _offered(card: str) -> list[str]:
    return re.findall(r'data-sport-option="([^"]+)"', card)


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _final(body: str) -> str:
    events = [block for block in body.replace("\r\n", "\n").split("\n\n") if block.strip()]
    return "\n".join(
        line.removeprefix("data: ") for line in events[-1].split("\n") if line.startswith("data:")
    )


async def test_the_card_offers_only_the_athletes_other_loggable_sports() -> None:
    """Climbing isn't the athlete's and rowing has no extraction, so cycling is all."""
    setup = _App(sports=("gym", UNLOGGABLE_SPORT, "cycling"))
    draft = await setup.gym_draft()

    card = _card(setup.client.get(f"/drafts/{draft.id}", headers=_HTMX).text)

    assert _offered(card) == ["cycling"]
    assert "data-sport-chip" in card
    assert str(render_icon("bike", "size-4")) in card
    assert "Gym , change sport" in _text(card)
    # A pick disables every button of the card while it runs, so a second tap posts nothing.
    assert f"hx-disable=\"[data-draft-id='{draft.id}'] button\"" in card


async def test_a_one_sport_athlete_gets_no_chip() -> None:
    setup = _App(sports=("gym",))
    draft = await setup.gym_draft()

    card = _card(setup.client.get(f"/drafts/{draft.id}", headers=_HTMX).text)

    assert "data-sport-chip" not in card
    assert _offered(card) == []
    assert "Gym · draft" in card


async def test_the_chip_shows_on_the_streamed_card_and_after_a_reload() -> None:
    setup = _App()
    log = await setup.log()

    streamed = _card(_final(setup.client.get(f"/messages/{log.id}/reply").text))
    reloaded = _card(setup.client.get("/").text)

    assert _offered(streamed) == _offered(reloaded) == ["cycling"]


async def test_switching_a_gym_draft_to_cycling_answers_with_a_cycling_card_from_the_text() -> None:
    setup = _App()
    setup.conversation.extractions = [_RIDE]
    draft = await setup.gym_draft()

    response = setup.switch(draft.id, "cycling")

    assert response.status_code == 200
    card = _card(response.text)
    assert f'data-draft-id="{draft.id}"' in card
    assert 'aria-label="Rides"' in card
    assert "45 min Moving" in _text(card)
    assert _offered(card) == ["gym"]
    ((context, sport_id),) = setup.conversation.extracted
    assert (context.message, sport_id) == (_INTERVALS, "cycling")
    (stored,) = setup.drafts.all_of(setup.user_id)
    assert (stored.id, stored.sport_id, stored.status) == (draft.id, "cycling", "pending")


async def test_the_switched_card_stays_under_its_reply_after_a_reload() -> None:
    """The reply part is gym's extraction line, but the draft under it is now a ride."""
    setup = _App()
    setup.conversation.extractions = [_RIDE]
    draft = await setup.gym_draft()
    setup.switch(draft.id, "cycling")

    page = setup.client.get("/").text

    card = _card(page)
    assert 'aria-label="Rides"' in card
    assert page.index("Here&#39;s the session.") < page.index(f'data-draft-id="{draft.id}"')


async def test_the_switch_writes_no_session_and_confirm_saves_the_new_sport() -> None:
    setup = _App()
    setup.conversation.extractions = [_RIDE]
    draft = await setup.gym_draft()

    setup.switch(draft.id, "cycling")
    assert setup.drafts.confirmed_at == {}
    confirmed = setup.client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    assert confirmed.status_code == 200
    assert "Logged 45 min ride to Fri 2 Oct" in _text(confirmed.text)
    (stored,) = setup.drafts.all_of(setup.user_id)
    assert (stored.sport_id, stored.status) == ("cycling", "confirmed")


@pytest.mark.parametrize("sport_id", ["climbing", "gym", UNLOGGABLE_SPORT, "", "nope"])
async def test_a_sport_the_card_never_offered_is_a_422_with_no_call(sport_id: str) -> None:
    setup = _App(sports=("gym", "cycling", UNLOGGABLE_SPORT))
    draft = await setup.gym_draft()

    response = setup.switch(draft.id, sport_id)

    assert response.status_code == 422
    assert setup.conversation.extracted == []
    assert setup.drafts.all_of(setup.user_id) == [draft]


@pytest.mark.parametrize("outcome", [LlmCallOutcome.TIMEOUT, LlmCallOutcome.ERROR])
async def test_a_failed_switch_keeps_the_gym_card_with_a_note_to_pick_again(
    outcome: LlmCallOutcome,
) -> None:
    setup = _App()
    setup.conversation.extract_outcomes = [outcome]
    draft = await setup.gym_draft()

    response = setup.switch(draft.id, "cycling")

    assert response.status_code == 200
    card = _card(response.text)
    assert 'aria-label="Exercises"' in card
    assert re.search(r"role=\"alert\"[^>]*>Couldn't change the sport", card)
    assert _offered(card) == ["cycling"]
    assert setup.drafts.all_of(setup.user_id) == [draft]


async def test_a_retry_after_a_failure_switches_the_draft() -> None:
    setup = _App()
    setup.conversation.extractions = [_RIDE]
    setup.conversation.extract_outcomes = [LlmCallOutcome.TIMEOUT, LlmCallOutcome.SUCCESS]
    draft = await setup.gym_draft()

    setup.switch(draft.id, "cycling")
    retried = setup.switch(draft.id, "cycling")

    assert 'aria-label="Rides"' in _card(retried.text)
    assert "Couldn" not in retried.text


@pytest.mark.parametrize("status", ["confirmed", "discarded"])
async def test_switching_a_resolved_draft_is_a_409_with_no_call(status: str) -> None:
    setup = _App()
    draft = await setup.gym_draft()
    setup.drafts.set_status(draft.id, status)  # type: ignore[arg-type]

    assert setup.switch(draft.id, "cycling").status_code == 409
    assert setup.conversation.extracted == []


async def test_another_users_unknown_or_malformed_draft_is_a_404() -> None:
    owner = _App()
    draft = await owner.gym_draft()
    stranger = _App()
    stranger.drafts.rows = owner.drafts.rows

    assert stranger.switch(draft.id, "cycling").status_code == 404
    assert owner.switch(uuid4(), "cycling").status_code == 404
    assert owner.switch("not-a-uuid", "cycling").status_code == 404
    assert stranger.conversation.extracted == owner.conversation.extracted == []
    assert owner.drafts.all_of(owner.user_id) == [draft]


async def test_a_switch_without_the_csrf_token_is_refused_and_changes_nothing() -> None:
    setup = _App()
    setup.app.add_middleware(
        CsrfMiddleware, secret_key=b"unit-test-secret", templates=build_templates()
    )
    draft = await setup.gym_draft()
    client = TestClient(setup.app)
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))

    response = client.post(f"/drafts/{draft.id}/sport", data={"sport_id": "cycling"}, headers=_HTMX)

    assert response.status_code == 403
    assert setup.drafts.all_of(setup.user_id) == [draft]
