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
from pydantic import JsonValue

from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.sports.base import DateSource
from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.sports.gym.payload import GymPayload
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
    return await _pending(
        chat,
        drafts,
        user_id,
        extraction.to_payload().model_dump(mode="json"),
        occurred_on=extraction.occurred_on,
        date_source=extraction.date_source,
    )


async def _pending(
    chat: InMemoryChatRepository,
    drafts: InMemoryDraftRepository,
    user_id: UUID,
    payload: dict[str, JsonValue],
    *,
    occurred_on: date = _TODAY,
    date_source: DateSource = "assumed_today",
) -> StoredDraft:
    """The same, for a gym payload given as stored."""
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
            occurred_on=occurred_on,
            date_source=date_source,
            timezone="Europe/Warsaw",
            payload=payload,
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
    assert (stored.sport_id, stored.payload_version) == ("gym", 4)


async def test_a_typed_pyramid_ends_with_one_bench_block_on_the_card() -> None:
    pyramid = GymExtraction.model_validate(
        {
            "occurred_on": _TODAY,
            "date_source": "assumed_today",
            "activities": [
                {"name": "bench", "sets": 1, "reps": reps, "load": {"original": kg, "value": kg}}
                for reps, kg in ((10, "60"), (8, "70"), (6, "80"))
            ],
        }
    )
    client, user_id, chat, _ = _client(pyramid)
    asked = await _logged(chat, user_id, "bench 10 @ 60, 8 @ 70, 6 @ 80")

    card = _card(_final(client.get(f"/messages/{asked.id}/reply").text))

    (bench,) = _exercises(card)
    assert _text(bench) == "Bench 10 @ 60 kg · 8 @ 70 kg · 6 @ 80 kg"


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
        (BENCH_YESTERDAY, "Bench"),
        (
            GymExtraction.model_validate(
                {
                    "occurred_on": _TODAY,
                    "date_source": "assumed_today",
                    "activities": [{"name": "pull-ups", "sets": 3}],
                }
            ),
            "Pull-ups",
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


_PYRAMID_PAYLOAD: dict[str, JsonValue] = {
    "sport": "gym",
    "activities": [
        {
            "name_as_typed": "bench",
            "measure": "load_reps",
            "rir": 2,
            "sets": [
                {"reps": 10, "load": {"original": "60", "value": 60, "unit": "kg", "kg": 60}},
                {"reps": 8, "load": {"original": "70 kg", "value": 70, "unit": "kg", "kg": 70}},
                {"reps": 6, "load": {"original": "80", "value": 80, "unit": "kg", "kg": 80}},
            ],
        }
    ],
}
_PLANK_AND_RED_BAND_ROWS: dict[str, JsonValue] = {
    "sport": "gym",
    "activities": [
        {"name_as_typed": "plank", "measure": "time", "sets": [{"duration_s": 45}] * 3},
        {
            "name_as_typed": "rows",
            "measure": "band_reps",
            "sets": [{"reps": 10, "band_label": "red"}] * 3,
        },
    ],
}
_EDIT_BASE = {"occurred_on": "2026-10-02", "duration_min": "", "rpe": "", "feeling": ""}
_KG_6 = {"original": "6", "value": 6, "unit": "kg", "kg": 6}
_KG_24 = {"original": "24", "value": 24, "unit": "kg", "kg": 24}
_KG_60 = {"original": "60", "value": 60, "unit": "kg", "kg": 60}


def _set_fields(exercise: int, index: int, **fields: str) -> dict[str, str]:
    blank = {"reps": "", "load": "", "duration": "", "distance": "", "band": ""}
    return {
        f"activities.{exercise}.sets.{index}.{name}": v for name, v in {**blank, **fields}.items()
    }


def _pyramid_form(**changes: str) -> dict[str, str]:
    """What the edit form posts for `_PYRAMID_PAYLOAD` as shown, with `changes` over it."""
    form = {
        **_EDIT_BASE,
        "activities.0.name": "bench",
        "activities.0.measure": "load_reps",
        "activities.0.per_side": "0",
        "activities.0.implements": "",
        "activities.0.rir": "2",
        **_set_fields(0, 0, reps="10", load="60"),
        **_set_fields(0, 1, reps="8", load="70"),
        **_set_fields(0, 2, reps="6", load="80"),
    }
    return {**form, **changes}


def _stored_exercise(drafts: InMemoryDraftRepository, user_id: UUID) -> dict[str, JsonValue]:
    (stored,) = drafts.all_of(user_id)
    activities = stored.draft.payload["activities"]
    assert isinstance(activities, list)
    exercise = activities[0]
    assert isinstance(exercise, dict)
    return exercise


def _stored_sets(drafts: InMemoryDraftRepository, user_id: UUID) -> list[JsonValue]:
    sets = _stored_exercise(drafts, user_id)["sets"]
    assert isinstance(sets, list)
    return sets


async def test_a_plank_draft_shows_3_x_45_s_and_its_logged_card_shows_the_same() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _pending(chat, drafts, user_id, _PLANK_AND_RED_BAND_ROWS)

    plank, rows = _exercises(_card(client.get(f"/drafts/{draft.id}", headers=_HTMX).text))
    logged = _text(client.post(f"/drafts/{draft.id}/confirm", headers=_HTMX).text)

    assert _text(plank) == f"Plank 3 {_TIMES} 45 s"
    assert _text(rows) == f"Rows 3 {_TIMES} 10 · red band"
    assert f"Plank 3 {_TIMES} 45 s" in logged
    assert f"Rows 3 {_TIMES} 10 · red band" in logged
    assert "Logged 2 exercises to Fri 2 Oct" in logged


@pytest.mark.parametrize(
    ("exercise", "shown"),
    [
        (
            {"per_side": True, "rir": 2, "sets": [{"reps": 12}] * 3},
            f"Lunge 3 {_TIMES} 12 each side · RIR 2",
        ),
        (
            {"implements": 2, "sets": [{"reps": 12, "load": _KG_6}] * 3},
            f"Lunge 3 {_TIMES} 12 · 2 {_TIMES} 6 kg",
        ),
        (
            {
                "per_side": True,
                "implements": 2,
                "rir": 2,
                "sets": [{"reps": 12, "load": _KG_6}] * 3,
            },
            f"Lunge 3 {_TIMES} 12 each side · 2 {_TIMES} 6 kg · RIR 2",
        ),
        (
            {"implements": 2, "sets": [{"distance_m": 40, "load": _KG_24}] * 2},
            f"Lunge 2 {_TIMES} 40 m · 2 {_TIMES} 24 kg",
        ),
        ({"sets": [{"reps": 10, "load": _KG_60}]}, "Lunge 10 reps · 60 kg"),
        ({"sets": [{}] * 4}, "Lunge 4 sets"),
        ({"sets": [{"duration_s": 30}]}, "Lunge 30 s"),
        ({"per_side": True, "sets": [{}] * 3}, "Lunge 3 sets each side"),
        (
            {"per_side": True, "implements": 2, "sets": [{"load": _KG_6}]},
            f"Lunge each side · 2 {_TIMES} 6 kg",
        ),
        ({"sets": [{"distance_m": 1234567}]}, "Lunge 1234567 m"),
    ],
)
async def test_sides_implements_and_the_other_measures_show_on_the_card(
    exercise: dict[str, JsonValue], shown: str
) -> None:
    client, user_id, chat, drafts = _client()
    payload: dict[str, JsonValue] = {
        "sport": "gym",
        "activities": [{"name_as_typed": "lunge", "measure": "reps", **exercise}],
    }
    draft = await _pending(chat, drafts, user_id, payload)

    (row,) = _exercises(_card(client.get(f"/drafts/{draft.id}", headers=_HTMX).text))

    assert _text(row) == shown


async def test_a_long_distance_prefills_the_form_exactly_not_in_exponent_form() -> None:
    client, user_id, chat, drafts = _client()
    payload: dict[str, JsonValue] = {
        "sport": "gym",
        "activities": [
            {"name_as_typed": "ride", "measure": "distance", "sets": [{"distance_m": 1234567.5}]}
        ],
    }
    draft = await _pending(chat, drafts, user_id, payload)

    form = client.get(f"/drafts/{draft.id}/edit", headers=_HTMX).text

    assert re.search(r'name="activities\.0\.sets\.0\.distance"[^>]*value="1234567\.5"', form)


async def test_a_pyramid_in_one_entry_shows_each_sets_reps_at_its_load() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _pending(chat, drafts, user_id, _PYRAMID_PAYLOAD)

    (row,) = _exercises(_card(client.get(f"/drafts/{draft.id}", headers=_HTMX).text))

    assert _text(row) == "Bench 10 @ 60 kg · 8 @ 70 kg · 6 @ 80 kg · RIR 2"


async def test_a_value_the_log_didnt_give_is_left_out_of_the_card_and_blank_in_the_form() -> None:
    client, user_id, chat, drafts = _client()
    payload: dict[str, JsonValue] = {
        "sport": "gym",
        "activities": [{"name_as_typed": "lunge", "measure": "reps", "sets": [{"reps": 8}, {}]}],
    }
    draft = await _pending(chat, drafts, user_id, payload)

    (row,) = _exercises(_card(client.get(f"/drafts/{draft.id}", headers=_HTMX).text))
    form = client.get(f"/drafts/{draft.id}/edit", headers=_HTMX).text

    assert _text(row) == "Lunge 8"
    for field in ("reps", "load", "duration", "distance", "band"):
        name = rf"activities\.0\.sets\.1\.{field}"
        assert re.search(rf'<input[^>]*name="{name}"[^>]*value=""', form), field
    assert not re.search(r'<input[^>]*name="activities\.0\.(implements|rir)"[^>]*value="0"', form)


async def test_the_edit_form_has_each_exercises_fields_and_a_row_per_set() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _pending(chat, drafts, user_id, _PYRAMID_PAYLOAD)

    form = client.get(f"/drafts/{draft.id}/edit", headers=_HTMX).text

    assert re.search(r'<input[^>]*name="activities\.0\.name"[^>]*value="bench"', form)
    assert re.search(r'<select[^>]*name="activities\.0\.measure"', form)
    assert re.search(r'<option value="load_reps" selected', form)
    assert re.search(r'<input[^>]*type="checkbox"[^>]*name="activities\.0\.per_side"', form)
    assert re.search(r'<input[^>]*name="activities\.0\.implements"', form)
    assert re.search(r'<input[^>]*name="activities\.0\.rir"[^>]*value="2"', form)
    for index, (reps, load) in enumerate((("10", "60"), ("8", "70"), ("6", "80"))):
        for name, value in (("reps", reps), ("load", load)):
            field = rf'name="activities\.0\.sets\.{index}\.{name}"[^>]*value="{value}"'
            assert re.search(rf"<input[^>]*{field}", form), (index, name)
    assert "activities.0.sets.3." not in form.split("<template")[0]
    assert "data-add-set" in form
    assert form.count("data-remove-set") >= 3


async def test_each_set_row_names_its_stored_set_and_a_lone_row_cannot_be_removed() -> None:
    client, user_id, chat, drafts = _client()
    payload: dict[str, JsonValue] = {
        "sport": "gym",
        "activities": [
            {"name_as_typed": "bench", "measure": "reps", "sets": [{"reps": 5}, {"reps": 3}]},
            {"name_as_typed": "plank", "measure": "time", "sets": [{"duration_s": 45}]},
        ],
    }
    draft = await _pending(chat, drafts, user_id, payload)

    form = client.get(f"/drafts/{draft.id}/edit", headers=_HTMX).text

    assert re.search(r'<input type="hidden" name="activities\.0\.sets\.0\.src" value="0"', form)
    assert re.search(r'<input type="hidden" name="activities\.0\.sets\.1\.src" value="1"', form)
    assert re.search(
        r'<input type="hidden" name="activities\.0\.sets\.__set__\.src" value=""', form
    )
    removes = re.findall(r"<button[^>]*data-remove-set[^>]*>", form)
    # Bench's two sets and its template row, then the plank's lone set.
    assert [" disabled" in button for button in removes[:4]] == [False, False, False, True]


async def test_a_crafted_set_key_cannot_ask_for_a_hundred_thousand_rows() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _pending(chat, drafts, user_id, _PYRAMID_PAYLOAD)

    response = client.post(
        f"/drafts/{draft.id}/edit",
        headers=_HTMX,
        data=_pyramid_form(
            **_set_fields(0, 1, reps="8", load="heavy"),
            **{"activities.0.sets.99999.reps": "1"},
        ),
    )

    assert response.status_code == 422
    assert response.text.count('name="activities.0.sets.100.reps"') == 0
    assert response.text.count("data-set-row") <= 101 + 4


async def test_an_unticked_each_side_posted_after_its_hidden_zero_stays_off() -> None:
    client, user_id, chat, drafts = _client()
    payload: dict[str, JsonValue] = {
        "sport": "gym",
        "activities": [
            {
                "name_as_typed": "lunge",
                "measure": "reps",
                "per_side": True,
                "sets": [{"reps": 12}],
            }
        ],
    }
    draft = await _pending(chat, drafts, user_id, payload)
    unticked = {
        **_EDIT_BASE,
        "activities.0.name": "lunge",
        "activities.0.per_side": "0",
        **_set_fields(0, 0, reps="12"),
    }
    ticked = {**unticked, "activities.0.per_side": ["0", "1"]}

    client.post(f"/drafts/{draft.id}/edit", headers=_HTMX, data=unticked)
    off = _stored_exercise(drafts, user_id)["per_side"]
    client.post(f"/drafts/{draft.id}/edit", headers=_HTMX, data=ticked)
    on = _stored_exercise(drafts, user_id)["per_side"]

    assert (off, on) == (False, True)


async def test_editing_one_set_of_a_pyramid_changes_only_that_set() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _pending(chat, drafts, user_id, _PYRAMID_PAYLOAD)
    old = GymPayload.model_validate(_PYRAMID_PAYLOAD).model_dump(mode="json")["activities"][0][
        "sets"
    ]

    response = client.post(
        f"/drafts/{draft.id}/edit",
        headers=_HTMX,
        data=_pyramid_form(**_set_fields(0, 1, reps="7", load="72.5")),
    )

    assert response.status_code == 200
    sets = _stored_sets(drafts, user_id)
    assert [sets[0], sets[2]] == [old[0], old[2]]
    middle = sets[1]
    assert isinstance(middle, dict)
    assert middle["reps"] == 7
    assert middle["load"] == {"original": "72.5 kg", "value": 72.5, "unit": "kg", "kg": 72.5}
    assert "Bench 10 @ 60 kg · 7 @ 72.5 kg · 6 @ 80 kg · RIR 2" in _text(_card(response.text))


async def test_clearing_a_value_saves_it_as_null() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _pending(chat, drafts, user_id, _PYRAMID_PAYLOAD)

    response = client.post(
        f"/drafts/{draft.id}/edit",
        headers=_HTMX,
        data=_pyramid_form(**_set_fields(0, 0), **{"activities.0.rir": ""}),
    )

    assert response.status_code == 200
    assert _stored_sets(drafts, user_id)[0] == {
        "reps": None,
        "load": None,
        "duration_s": None,
        "distance_m": None,
        "band_label": None,
    }
    assert _stored_exercise(drafts, user_id)["rir"] is None


async def test_a_set_added_in_the_form_is_saved_and_a_removed_one_is_gone() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _pending(chat, drafts, user_id, _PYRAMID_PAYLOAD)
    data = {
        key: value
        for key, value in _pyramid_form().items()
        if not key.startswith("activities.0.sets.1.")
    }
    data = {key.replace("sets.2.", "sets.1."): value for key, value in data.items()}
    data.update(_set_fields(0, 2, reps="4", load="90"))

    response = client.post(f"/drafts/{draft.id}/edit", headers=_HTMX, data=data)

    assert response.status_code == 200
    assert "Bench 10 @ 60 kg · 6 @ 80 kg · 4 @ 90 kg · RIR 2" in _text(_card(response.text))


async def test_saving_an_untouched_load_keeps_the_lb_as_typed() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _draft(chat, drafts, user_id, DEADLIFT_IN_LB)
    (before,) = drafts.all_of(user_id)
    old = dict(before.draft.payload)
    sets = {
        key: value
        for index in range(3)
        for key, value in _set_fields(0, index, reps="5", load="142.88").items()
    }

    response = client.post(
        f"/drafts/{draft.id}/edit",
        headers=_HTMX,
        data={**_EDIT_BASE, "activities.0.name": "deadlift", "activities.0.rir": "2", **sets},
    )

    assert response.status_code == 200
    (stored,) = drafts.all_of(user_id)
    assert stored.draft.payload == old
    assert f"Deadlift 3 {_TIMES} 5 · 142.9 kg · RIR 2" in _text(_card(response.text))


async def test_a_rejected_set_value_is_marked_in_its_row_and_writes_nothing() -> None:
    client, user_id, chat, drafts = _client()
    draft = await _pending(chat, drafts, user_id, _PYRAMID_PAYLOAD)

    response = client.post(
        f"/drafts/{draft.id}/edit",
        headers=_HTMX,
        data=_pyramid_form(**_set_fields(0, 1, reps="8", load="heavy")),
    )

    assert response.status_code == 422
    assert re.search(
        r'<input[^>]*name="activities\.0\.sets\.1\.load"[^>]*value="heavy"[^>]*aria-invalid="true"',
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
        ("activities.0.sets.2.reps", "5"),
        ("activities.0.sets.2.load", "142.88"),
    ):
        assert re.search(rf'<input[^>]*name="{re.escape(name)}"[^>]*value="{value}"', form), name
