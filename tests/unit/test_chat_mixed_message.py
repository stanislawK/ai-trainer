"""A mixed message in the chat (PRD-0005 B1, B14, F15, ticket #161): its stream ends with one
draft card per sport, in message order, each confirmed on its own. A sport whose extraction
failed shows a "couldn't draft" row with Retry instead of its card, after a reload too, and
Retry swaps in that sport's card. The reply shows no sport glyph of its own: a part about
several sports, or a failed part, carries the app mark."""

import re
from datetime import date
from html import unescape
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CsrfMiddleware
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    THREE_6A_TWO_6B,
    InMemoryDraftRepository,
    InMemoryProfile,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}
_MIXED = "Bouldered 4 6B then rode home 15 km"
_HOME_15_KM = CyclingExtraction.model_validate(
    {
        "occurred_on": date(2026, 10, 2),
        "date_source": "assumed_today",
        "activities": [{"distance": {"original": "15 km", "value": 15, "unit": "km"}}],
    }
)
_AVATAR = re.compile(r'<span data-avatar="([^"]+)"')
_CARD = re.compile(r'<article[^>]*aria-label="Draft session".*?</article>', re.S)
_FAILED_ROW = re.compile(r"<div[^>]*data-draft-failed=\"([^\"]+)\".*?</div>\s*</div>", re.S)


class _App:
    def __init__(self, *, failing: tuple[str, ...] = ()) -> None:
        app = FastAPI()
        self.drafts = InMemoryDraftRepository()
        self.profile = InMemoryProfile()
        self.conversation = ScriptedConversation(
            intents=[
                [
                    Intent(kind="log_session", sport="climbing"),
                    Intent(kind="log_session", sport="cycling"),
                ]
            ],
            sport_extractions={"climbing": THREE_6A_TWO_6B, "cycling": _HOME_15_KM},
            sport_outcomes={
                sport: [LlmCallOutcome.ERROR, LlmCallOutcome.SUCCESS] for sport in failing
            },
        )
        self.chat = include_chat(
            app,
            build_templates(),
            conversation=self.conversation,
            drafts=self.drafts,
            profile=self.profile,
        )
        self.user_id = sign_in_as(app).id
        self.profile.sports[self.user_id] = ["climbing", "cycling"]
        self.app = app
        self.client = TestClient(app)

    async def log(self) -> ChatMessage:
        return await self.chat.add(
            self.user_id,
            await self.chat.conversation_of(self.user_id),
            ChatRole.USER,
            _MIXED,
            FROZEN_NOW,
        )

    def stream(self, message_id: UUID) -> str:
        return _final(self.client.get(f"/messages/{message_id}/reply").text)

    def retry(self, part_id: UUID | str):  # type: ignore[no-untyped-def]
        return self.client.post(f"/messages/{part_id}/retry-draft", headers=_HTMX)

    async def failed_part(self, message: ChatMessage) -> ChatMessage:
        (part,) = [
            part
            for part in await self.chat.list_replies(self.user_id, message)
            if part.template_id == "draft_failed"
        ]
        return part


def _final(body: str) -> str:
    events = [block for block in body.replace("\r\n", "\n").split("\n\n") if block.strip()]
    return "\n".join(
        line.removeprefix("data: ") for line in events[-1].split("\n") if line.startswith("data:")
    )


# Each sport's card body names its list; the header's sport is a chip for a two-sport athlete.
_BODIES = {'aria-label="Activities"': "Climbing", 'aria-label="Rides"': "Cycling"}


def _card_sports(html: str) -> list[str]:
    cards = _CARD.findall(html)
    return [sport for card in cards for label, sport in _BODIES.items() if label in card]


def _text(html: str) -> str:
    return unescape(" ".join(re.sub(r"<[^>]+>", " ", html).split()))


async def test_the_mixed_message_ends_with_a_climbing_and_a_cycling_card_in_order() -> None:
    setup = _App()
    asked = await setup.log()

    html = setup.stream(asked.id)

    assert _card_sports(html) == ["Climbing", "Cycling"]
    climb, ride = setup.drafts.all_of(setup.user_id)
    assert html.index(f'data-draft-id="{climb.id}"') < html.index(f'data-draft-id="{ride.id}"')


async def test_the_mixed_reply_shows_no_sport_glyph() -> None:
    """F15: a reply about several sports never shows one sport's glyph."""
    setup = _App()
    asked = await setup.log()

    html = setup.stream(asked.id)

    assert set(_AVATAR.findall(html)) <= {"app-mark"}


async def test_confirming_one_card_leaves_the_other_pending_on_the_page() -> None:
    setup = _App()
    asked = await setup.log()
    setup.stream(asked.id)
    climb, ride = setup.drafts.all_of(setup.user_id)

    confirmed = setup.client.post(f"/drafts/{climb.id}/confirm", headers=_HTMX)

    assert confirmed.status_code == 200
    assert f'data-draft-id="{ride.id}"' not in confirmed.text
    page = setup.client.get("/").text
    assert _card_sports(page) == ["Cycling"]
    assert [draft.status for draft in setup.drafts.all_of(setup.user_id)] == [
        "confirmed",
        "pending",
    ]


async def test_a_failed_ride_shows_the_climbing_card_and_a_retry_row() -> None:
    setup = _App(failing=("cycling",))
    asked = await setup.log()

    html = setup.stream(asked.id)

    assert _card_sports(html) == ["Climbing"]
    (sport,) = _FAILED_ROW.findall(html)
    assert sport == "cycling"
    row = _FAILED_ROW.search(html)
    assert row is not None
    assert "Cycling" in _text(row.group(0))
    assert "couldn't draft" in _text(row.group(0))
    failed = await setup.failed_part(asked)
    assert f'hx-post="/messages/{failed.id}/retry-draft"' in row.group(0)
    assert re.search(r">\s*Retry\s*<", row.group(0))
    assert set(_AVATAR.findall(html)) == {"app-mark"}


async def test_the_retry_row_stays_after_a_reload() -> None:
    setup = _App(failing=("cycling",))
    asked = await setup.log()
    setup.stream(asked.id)

    page = setup.client.get("/").text

    assert _card_sports(page) == ["Climbing"]
    assert _FAILED_ROW.findall(page) == ["cycling"]


async def test_retry_swaps_in_the_cycling_card() -> None:
    setup = _App(failing=("cycling",))
    asked = await setup.log()
    setup.stream(asked.id)

    response = setup.retry((await setup.failed_part(asked)).id)

    assert response.status_code == 200
    assert _card_sports(response.text) == ["Cycling"]
    assert _FAILED_ROW.findall(response.text) == []
    page = setup.client.get("/").text
    assert _card_sports(page) == ["Climbing", "Cycling"]
    assert _FAILED_ROW.findall(page) == []


async def test_a_failed_retry_keeps_the_row_with_a_note() -> None:
    setup = _App()
    setup.conversation.sport_outcomes = {"cycling": [LlmCallOutcome.ERROR]}
    asked = await setup.log()
    setup.stream(asked.id)

    response = setup.retry((await setup.failed_part(asked)).id)

    assert response.status_code == 200
    assert _FAILED_ROW.findall(response.text) == ["cycling"]
    assert re.search(r"role=\"alert\"[^>]*>Still couldn't", response.text)
    assert [draft.sport_id for draft in setup.drafts.all_of(setup.user_id)] == ["climbing"]


async def test_retrying_a_part_that_did_not_fail_is_a_404_with_no_call() -> None:
    setup = _App(failing=("cycling",))
    asked = await setup.log()
    setup.stream(asked.id)
    climb = (await setup.chat.list_replies(setup.user_id, asked))[0]
    calls = len(setup.conversation.extracted)

    assert setup.retry(climb.id).status_code == 404
    assert setup.retry(asked.id).status_code == 404
    assert len(setup.conversation.extracted) == calls


async def test_another_users_unknown_or_malformed_part_is_a_404() -> None:
    owner = _App(failing=("cycling",))
    asked = await owner.log()
    owner.stream(asked.id)
    failed = await owner.failed_part(asked)
    stranger = _App()
    stranger.chat.rows = owner.chat.rows

    assert stranger.retry(failed.id).status_code == 404
    assert owner.retry(uuid4()).status_code == 404
    assert owner.retry("not-a-uuid").status_code == 404
    assert stranger.conversation.extracted == []
    assert [draft.sport_id for draft in owner.drafts.all_of(owner.user_id)] == ["climbing"]


async def test_a_retry_without_the_csrf_token_is_refused_and_changes_nothing() -> None:
    setup = _App(failing=("cycling",))
    setup.app.add_middleware(
        CsrfMiddleware, secret_key=b"unit-test-secret", templates=build_templates()
    )
    asked = await setup.log()
    failed = await setup.chat.add(
        setup.user_id,
        asked.conversation_id,
        ChatRole.ASSISTANT,
        "I couldn't draft this one.",
        FROZEN_NOW,
        sports=("cycling",),
        template_id="draft_failed",
        template_version=1,
    )
    client = TestClient(setup.app)
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))

    response = client.post(f"/messages/{failed.id}/retry-draft", headers=_HTMX)

    assert response.status_code == 403
    assert setup.conversation.extracted == []
    assert setup.drafts.all_of(setup.user_id) == []


async def test_a_failed_part_of_a_sport_no_longer_known_shows_as_plain_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    setup = _App()
    asked = await setup.log()
    await setup.chat.add(
        setup.user_id,
        asked.conversation_id,
        ChatRole.ASSISTANT,
        "I couldn't draft this one.",
        FROZEN_NOW,
        sports=("kayaking",),
        template_id="draft_failed",
        template_version=1,
    )

    page = setup.client.get("/")

    assert page.status_code == 200
    assert _FAILED_ROW.findall(page.text) == []
    assert "failed draft" in caplog.text
    assert "kayaking" in caplog.text
