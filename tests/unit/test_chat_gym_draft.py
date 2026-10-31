"""A gym log in the chat (PRD-0004 B1, F2, G4, ticket #159): it ends its stream with a gym
draft card of one row per exercise, its confirm collapses to the "Logged … to <date>" row as the
other sports do, and its edit form carries each exercise's name, sets, reps, load and RIR. No
core code knows the sport: the cards are the plugin's own partials."""

import re
from datetime import date, timedelta
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.sports.gym.extraction import GymExtraction
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
_LIFT_INTENTS = [[Intent(kind="log_session", sport="gym")]]
_TODAY = date(2026, 10, 2)
_TIMES = "\N{MULTIPLICATION SIGN}"
_SQUAT_AND_PULL_UPS = "5x5 squat at 100kg, then 3 sets of pull-ups"
SQUAT_AND_PULL_UPS = GymExtraction.model_validate(
    {
        "occurred_on": _TODAY,
        "date_source": "assumed_today",
        "activities": [
            {
                "name": "squat",
                "sets": 5,
                "reps": 5,
                "load": {"original": "100kg", "value": 100, "unit": "kg"},
            },
            {"name": "pull-ups", "sets": 3},
        ],
    }
)
BENCH_YESTERDAY = GymExtraction.model_validate(
    {
        "occurred_on": _TODAY - timedelta(days=1),
        "date_source": "relative",
        "activities": [
            {"name": "Bench", "sets": 3, "reps": 8, "load": {"original": "60", "value": 60}}
        ],
    }
)
DEADLIFT_IN_LB = GymExtraction.model_validate(
    {
        "occurred_on": _TODAY,
        "date_source": "assumed_today",
        "activities": [
            {
                "name": "deadlift",
                "sets": 3,
                "reps": 5,
                "load": {"original": "315 lb", "value": 315, "unit": "lb"},
                "rir": 2,
            }
        ],
    }
)


def _client(
    extraction: GymExtraction = SQUAT_AND_PULL_UPS,
) -> tuple[TestClient, UUID, InMemoryChatRepository, InMemoryDraftRepository]:
    app = FastAPI()
    drafts = InMemoryDraftRepository()
    conversation = ScriptedConversation(intents=_LIFT_INTENTS, extractions=[extraction])
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
    extraction: GymExtraction,
) -> StoredDraft:
    """The athlete's gym log, its extraction reply, and the pending gym draft under it."""
    conversation_id = await chat.conversation_of(user_id)
    log = await chat.add(user_id, conversation_id, ChatRole.USER, "my lifts", FROZEN_NOW)
    await chat.add(
        user_id,
        conversation_id,
        ChatRole.ASSISTANT,
        "Here's the session.",
        FROZEN_NOW,
        sports=("gym",),
        template_id="gym.extract",
        template_version=1,
    )
    return await drafts.add(
        user_id,
        conversation_id,
        log.id,
        "gym",
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


def _exercises(card: str) -> list[str]:
    return re.findall(r"<li.*?</li>", card, re.S)


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


async def test_5x5_squat_then_3_sets_of_pull_ups_ends_with_a_gym_card_of_both() -> None:
    client, user_id, chat, drafts = _client()
    asked = await _logged(chat, user_id, _SQUAT_AND_PULL_UPS)

    card = _card(_final(client.get(f"/messages/{asked.id}/reply").text))

    assert "Gym · draft" in card
    assert str(render_icon("dumbbell", "size-4")) in card
    squat, pull_ups = _exercises(card)
    assert _text(squat) == f"Squat 5 {_TIMES} 5 · 100 kg"
    assert _text(pull_ups) == "Pull-ups 3 sets"
    (stored,) = drafts.all_of(user_id)
    assert (stored.sport_id, stored.payload_version) == ("gym", 3)


async def test_bench_yesterday_is_a_card_dated_yesterday() -> None:
    client, user_id, chat, _ = _client(BENCH_YESTERDAY)
    asked = await _logged(chat, user_id, f"Bench yesterday 3{_TIMES}8 @ 60")

    card = _card(_final(client.get(f"/messages/{asked.id}/reply").text))

    assert 'data-date-source="relative"' in card
    assert "Thu 1 Oct" in card
    assert "Assumed" not in card
    (bench,) = _exercises(card)
    assert _text(bench) == f"Bench 3 {_TIMES} 8 · 60 kg"


async def test_a_load_in_lb_shows_kg_and_what_was_typed() -> None:
    client, user_id, chat, _ = _client(DEADLIFT_IN_LB)
    asked = await _logged(chat, user_id, "deadlift 3x5 at 315 lb, 2 RIR")

    (deadlift,) = _exercises(_card(_final(client.get(f"/messages/{asked.id}/reply").text)))

    assert _text(deadlift) == f"Deadlift 3 {_TIMES} 5 · 142.9 kg · RIR 2 Typed: 315 lb"


@pytest.mark.parametrize(
    ("exercise", "shown"),
    [
        ({"name": "plank"}, "Plank"),
        ({"name": "dips", "reps": 12}, "Dips 12 reps"),
        ({"name": "row", "sets": 1, "reps": 1}, "Row 1 rep"),
        ({"name": "curl", "sets": 1}, "Curl"),
        ({"name": "RDL", "load": {"original": "80 kg", "value": 80, "unit": "kg"}}, "RDL 80 kg"),
    ],
)
async def test_an_exercise_shows_only_what_the_log_said(
    exercise: dict[str, object], shown: str
) -> None:
    extraction = GymExtraction.model_validate(
        {"occurred_on": _TODAY, "date_source": "assumed_today", "activities": [exercise]}
    )
    client, user_id, chat, _ = _client(extraction)
    asked = await _logged(chat, user_id, "my lifts")

    (row,) = _exercises(_card(_final(client.get(f"/messages/{asked.id}/reply").text)))

    assert _text(row) == shown


@pytest.mark.parametrize(
    ("extraction", "summary"),
    [
        (SQUAT_AND_PULL_UPS, "2 exercises"),
        (BENCH_YESTERDAY, f"Bench 3 {_TIMES} 8"),
        (
            GymExtraction.model_validate(
                {
                    "occurred_on": _TODAY,
                    "date_source": "assumed_today",
                    "activities": [{"name": "pull-ups", "sets": 3}],
                }
            ),
            "Pull-ups 3 sets",
        ),
        (
            GymExtraction.model_validate(
                {
                    "occurred_on": _TODAY,
                    "date_source": "assumed_today",
                    "activities": [{"name": "plank"}],
                }
            ),
            "Plank",
        ),
    ],
)
async def test_confirm_saves_the_lifts_and_collapses_to_their_logged_row(
    extraction: GymExtraction, summary: str
) -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id, extraction)
    day = "Thu 1 Oct" if extraction.occurred_on < _TODAY else "Fri 2 Oct"

    response = client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)

    assert response.status_code == 200
    assert f"Logged {summary} to {day}" in _text(response.text)
    (stored,) = drafts.all_of(user_id)
    assert (stored.status, drafts.confirmed_at) == ("confirmed", {draft.id: FROZEN_NOW})


async def test_confirmed_lifts_stay_logged_on_a_reload_and_their_undo_removes_them() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id, SQUAT_AND_PULL_UPS)
    client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX)
    (stored,) = drafts.all_of(user_id)

    reloaded = _text(client.get("/").text)
    undone = client.post(f"/drafts/{stored.id}/undo", headers=_HTMX)

    assert "Logged 2 exercises to Fri 2 Oct" in reloaded
    assert "Gym · draft" not in reloaded
    assert undone.status_code == 200
    assert "Removed" in _text(client.get("/").text)
    assert drafts.removed_sessions == [stored.session_id]


async def test_the_edit_form_carries_each_exercises_name_sets_reps_kg_and_rir() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id, DEADLIFT_IN_LB)

    form = client.get(f"/drafts/{draft.id}/edit", headers=_HTMX).text

    for name, value in (
        ("activities.0.name", "deadlift"),
        ("activities.0.sets", "3"),
        ("activities.0.reps", "5"),
        ("activities.0.load", "142.88"),
        ("activities.0.rir", "2"),
    ):
        assert re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*value="{value}"', form), name


async def test_saving_an_untouched_load_keeps_the_lb_as_typed() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id, DEADLIFT_IN_LB)

    response = client.post(
        f"/drafts/{draft.id}/edit",
        headers=_HTMX,
        data={
            "occurred_on": "2026-10-02",
            "duration_min": "",
            "rpe": "",
            "feeling": "",
            "activities.0.name": "deadlift",
            "activities.0.sets": "4",
            "activities.0.reps": "5",
            "activities.0.load": "142.88",
            "activities.0.rir": "2",
        },
    )

    assert response.status_code == 200
    (stored,) = drafts.all_of(user_id)
    payload = DEADLIFT_IN_LB.to_payload().model_dump(mode="json")
    deadlift = payload["activities"][0]
    assert stored.draft.payload == {
        **payload,
        "activities": [{**deadlift, "sets": [deadlift["sets"][0]] * 4}],
    }
    assert f"Deadlift 4 {_TIMES} 5 · 142.9 kg" in _text(_card(response.text))


async def test_a_rejected_load_comes_back_marked_and_writes_nothing() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id, DEADLIFT_IN_LB)

    response = client.post(
        f"/drafts/{draft.id}/edit",
        headers=_HTMX,
        data={"occurred_on": "2026-10-02", "activities.0.load": "heavy"},
    )

    assert response.status_code == 422
    assert re.search(
        r'<input[^>]*name="activities\.0\.load"[^>]*value="heavy"[^>]*aria-invalid="true"',
        response.text,
    )
    (stored,) = drafts.all_of(user_id)
    assert stored.draft == draft.draft


async def test_a_v1_draft_still_shows_its_card_and_its_edit_form() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id, DEADLIFT_IN_LB)
    (stored,) = drafts.all_of(user_id)
    stored.draft.payload.clear()
    stored.draft.payload.update(
        {
            "sport": "gym",
            "activities": [
                {
                    "name": "deadlift",
                    "sets": 3,
                    "reps": 5,
                    "load": {"original": "315 lb", "value": 315, "unit": "lb", "kg": 142.88},
                    "rir": 2,
                }
            ],
        }
    )

    form = client.get(f"/drafts/{draft.id}/edit", headers=_HTMX).text

    for name, value in (
        ("activities.0.name", "deadlift"),
        ("activities.0.sets", "3"),
        ("activities.0.reps", "5"),
        ("activities.0.load", "142.88"),
    ):
        assert re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*value="{value}"', form), name
