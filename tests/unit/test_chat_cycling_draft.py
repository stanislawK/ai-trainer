"""A cycling log in the chat (PRD-0004 B1, F2, G4, ticket #160): it ends its stream with a
cycling draft card of stat chips, its confirm collapses to the "Logged … to <date>" row as the
other sports do, and its edit form carries the ride's distance, moving time and elevation. No
core code knows the sport: the cards are the plugin's own partials."""

import re
from datetime import date
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction
from ai_trainer.domain.training import SessionDraft, StoredDraft
from ai_trainer.web.icons import render_icon
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    InMemoryChatRepository,
    InMemoryDraftRepository,
    ScriptedConversation,
    include_chat,
    sign_in_as,
)

_HTMX = {"HX-Request": "true"}
_RIDE_INTENTS = [[Intent(kind="log_session", sport="cycling")]]
_TODAY = date(2026, 10, 2)
_RODE_62 = "Rode 62 km with 800 m climbing this morning"
RODE_62_KM = CyclingExtraction.model_validate(
    {
        "occurred_on": _TODAY,
        "date_source": "relative",
        "activities": [
            {
                "distance": {"original": "62 km", "value": 62, "unit": "km"},
                "elevation": {"original": "800 m", "value": 800, "unit": "m"},
            }
        ],
    }
)
TWO_HOURS_EASY = CyclingExtraction.model_validate(
    {
        "occurred_on": _TODAY,
        "date_source": "assumed_today",
        "activities": [{"moving_time_min": 120}],
    }
)
FORTY_MILES = CyclingExtraction.model_validate(
    {
        "occurred_on": _TODAY,
        "date_source": "assumed_today",
        "activities": [
            {
                "distance": {"original": "40 miles", "value": 40, "unit": "mi"},
                "moving_time_min": 105,
                "elevation": {"original": "2000 ft", "value": 2000, "unit": "ft"},
                "zones": [{"zone": 2}],
            }
        ],
    }
)
TWO_RIDES = CyclingExtraction.model_validate(
    {
        "occurred_on": _TODAY,
        "date_source": "assumed_today",
        "activities": [{"moving_time_min": 25}, {"moving_time_min": 45}],
    }
)


def _client(
    extraction: CyclingExtraction = RODE_62_KM,
) -> tuple[TestClient, UUID, InMemoryChatRepository, InMemoryDraftRepository]:
    app = FastAPI()
    drafts = InMemoryDraftRepository()
    conversation = ScriptedConversation(intents=_RIDE_INTENTS, extractions=[extraction])
    chat = include_chat(app, build_templates(), conversation=conversation, drafts=drafts)
    user = sign_in_as(app)
    return TestClient(app), user.id, chat, drafts


async def _logged(chat: InMemoryChatRepository, user_id: UUID, text: str) -> ChatMessage:
    return await chat.add(
        user_id, await chat.conversation_of(user_id), ChatRole.USER, text, FROZEN_NOW
    )


async def _draft(
    chat: InMemoryChatRepository,
    drafts: InMemoryDraftRepository,
    user_id: UUID,
    extraction: CyclingExtraction,
) -> StoredDraft:
    """The athlete's ride log, its extraction reply, and the pending cycling draft under it."""
    conversation_id = await chat.conversation_of(user_id)
    log = await chat.add(user_id, conversation_id, ChatRole.USER, "my ride", FROZEN_NOW)
    await chat.add(
        user_id,
        conversation_id,
        ChatRole.ASSISTANT,
        "Here's the session.",
        FROZEN_NOW,
        sports=("cycling",),
        template_id="cycling.extract",
        template_version=1,
    )
    return await drafts.add(
        user_id,
        conversation_id,
        log.id,
        "cycling",
        SessionDraft(
            occurred_on=extraction.occurred_on,
            date_source=extraction.date_source,
            timezone="Europe/Warsaw",
            payload=extraction.to_payload().model_dump(mode="json"),
        ),
        payload_version=1,
        created_at=FROZEN_NOW,
    )


def _final(body: str) -> str:
    events = [block for block in body.replace("\r\n", "\n").split("\n\n") if block.strip()]
    return "\n".join(
        line.removeprefix("data: ") for line in events[-1].split("\n") if line.startswith("data:")
    )


def _card(html: str) -> str:
    card: str
    (card,) = re.findall(r'<article[^>]*aria-label="Draft session".*?</article>', html, re.S)
    return card


def _rides(card: str) -> list[str]:
    return re.findall(r"<li.*?</li>", card, re.S)


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


async def test_62_km_with_800_m_this_morning_ends_with_a_cycling_card_dated_today() -> None:
    client, user_id, chat, drafts = _client()
    asked = await _logged(chat, user_id, _RODE_62)

    card = _card(_final(client.get(f"/messages/{asked.id}/reply").text))

    assert "Cycling · draft" in card
    assert str(render_icon("bike", "size-4")) in card
    assert 'data-date-source="relative"' in card
    assert "Fri 2 Oct" in card
    assert "Assumed" not in card
    (ride,) = _rides(card)
    assert "62 km Distance" in _text(ride)
    assert "800 m Climbing" in _text(ride)
    (stored,) = drafts.all_of(user_id)
    assert (stored.sport_id, stored.payload_version) == ("cycling", 1)


async def test_two_hours_easy_shows_two_hours_of_moving_time() -> None:
    client, user_id, chat, _ = _client(TWO_HOURS_EASY)
    asked = await _logged(chat, user_id, "2 hours easy")

    (ride,) = _rides(_card(_final(client.get(f"/messages/{asked.id}/reply").text)))

    assert "2 h Moving" in _text(ride)
    assert "— Distance" in _text(ride)
    assert "— Climbing" in _text(ride)


async def test_a_ride_in_miles_shows_km_and_what_was_typed() -> None:
    client, user_id, chat, _ = _client(FORTY_MILES)
    asked = await _logged(chat, user_id, "40 miles, 2000 ft, 1:45 in Z2")

    (ride,) = _rides(_card(_final(client.get(f"/messages/{asked.id}/reply").text)))

    text = _text(ride)
    assert "64.4 km Distance" in text
    assert "1 h 45 min Moving" in text
    assert "610 m Climbing" in text
    assert "Typed: 40 miles · 2000 ft" in text
    assert "Z2" in text


async def test_several_rides_are_one_row_each() -> None:
    client, user_id, chat, _ = _client(TWO_RIDES)
    asked = await _logged(chat, user_id, "commute 25 min there and 45 back")

    rides = _rides(_card(_final(client.get(f"/messages/{asked.id}/reply").text)))

    assert ["25 min Moving" in _text(ride) for ride in rides] == [True, False]
    assert "45 min Moving" in _text(rides[1])


@pytest.mark.parametrize(
    ("extraction", "summary"),
    [
        (RODE_62_KM, "62 km ride"),
        (FORTY_MILES, "64.4 km ride"),
        (TWO_HOURS_EASY, "2 h ride"),
        (TWO_RIDES, "2 rides"),
        (
            CyclingExtraction.model_validate(
                {"occurred_on": _TODAY, "date_source": "assumed_today", "activities": [{}]}
            ),
            "ride",
        ),
    ],
)
async def test_confirm_saves_the_ride_and_collapses_to_its_logged_row(
    extraction: CyclingExtraction, summary: str
) -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id, extraction)

    response = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    assert response.status_code == 200
    assert f"Logged {summary} to Fri 2 Oct" in _text(response.text)
    (stored,) = drafts.all_of(user_id)
    assert (stored.status, drafts.confirmed_at) == ("confirmed", {draft.id: FROZEN_NOW})


async def test_a_confirmed_ride_stays_logged_on_a_reload_and_its_undo_removes_it() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id, FORTY_MILES)
    client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)
    (stored,) = drafts.all_of(user_id)

    reloaded = _text(client.get("/").text)
    undone = client.post(f"/sessions/{stored.session_id}/undo", headers=_HTMX)

    assert "Logged 64.4 km ride to Fri 2 Oct" in reloaded
    assert "Cycling · draft" not in reloaded
    assert undone.status_code == 200
    assert "Removed" in _text(client.get("/").text)
    assert drafts.removed_sessions == [stored.session_id]


async def test_the_edit_form_carries_each_rides_km_minutes_and_metres() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id, FORTY_MILES)

    form = client.get(f"/drafts/{draft.id}/edit", headers=_HTMX).text

    for name, value in (
        ("activities.0.distance", "64.37"),
        ("activities.0.moving_time_min", "105"),
        ("activities.0.elevation", "610"),
    ):
        assert re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*value="{value}"', form), name


async def test_saving_an_untouched_edit_form_keeps_the_miles_as_typed() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id, FORTY_MILES)

    response = client.post(
        f"/drafts/{draft.id}/edit",
        headers=_HTMX,
        data={
            "occurred_on": "2026-10-02",
            "duration_min": "",
            "rpe": "",
            "feeling": "",
            "activities.0.distance": "64.37",
            "activities.0.moving_time_min": "110",
            "activities.0.elevation": "610",
        },
    )

    assert response.status_code == 200
    (stored,) = drafts.all_of(user_id)
    assert stored.draft.payload == {
        **FORTY_MILES.to_payload().model_dump(mode="json"),
        "activities": [
            {
                **FORTY_MILES.to_payload().activities[0].model_dump(mode="json"),
                "moving_time_min": 110,
            }
        ],
    }
    assert "1 h 50 min Moving" in _text(_card(response.text))


async def test_a_rejected_distance_comes_back_marked_and_writes_nothing() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id, FORTY_MILES)

    response = client.post(
        f"/drafts/{draft.id}/edit",
        headers=_HTMX,
        data={"occurred_on": "2026-10-02", "activities.0.distance": "far"},
    )

    assert response.status_code == 422
    assert re.search(
        r'<input[^>]*name="activities\.0\.distance"[^>]*value="far"[^>]*aria-invalid="true"',
        response.text,
    )
    (stored,) = drafts.all_of(user_id)
    assert stored.draft == draft.draft
