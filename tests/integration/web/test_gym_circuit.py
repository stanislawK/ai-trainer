"""A circuit on the cards (PRD 0013 B43, ADR-0032 #618 and #632 amendments, ticket #632): the
draft, logged and History cards show one "N rounds of" block with its exercises, the edit forms
take one rounds value per circuit, posted as `activities.<i>.rounds` on the circuit's first
exercise, raising them copies each exercise's last round, and each exercise in a circuit keeps
its own sets, so its progress counts them. An entry outside any circuit and a v6 or v7 draft are
as before."""

import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.gym_session_reader import SqlAlchemyGymSessionReader
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.drafts import edit_draft
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.users import User
from ai_trainer.web.drafts import form_changes
from tests.integration import test_exercise_progress_route as progress_route
from tests.integration import test_history_route as history_route
from tests.integration.test_gym_activity_store import (
    _circuits,
    _id,
    _logged,
    circuit_draft,
    in_circuit,
)
from tests.integration.test_session_continuity_repository import _START, _Athlete, _athlete
from tests.unit import test_chat_gym_draft as chat_card

_EXERCISES: list[dict[str, Any]] = [
    in_circuit("burpees", 1, 10, 10, 10),
    in_circuit("squats", 1, 15, 15, 15),
    {"name_as_typed": "plank", "measure": "time", "sets": [{"duration_s": 45}]},
]
_BURPEES_AND_SQUATS: dict[str, JsonValue] = {"sport": "gym", "activities": list(_EXERCISES)}


def _rounds_blocks(html: str) -> list[str]:
    """The visible text from each "N rounds" heading to the next one or the end."""
    text = chat_card._text(html)
    return re.findall(r"\d+ rounds.*?(?=\d+ rounds|$)", text)


async def _user(athlete: _Athlete) -> User:
    user = await SqlAlchemyUsersRepository(athlete.factory).get(athlete.user_id)
    assert user is not None
    return user


async def _logged_circuit(
    factory: Callable[[], AsyncSession], sub: str
) -> tuple[_Athlete, UUID, TestClient]:
    athlete = await _athlete(factory, sub)
    logged = await _logged(athlete, "3 rounds: 10 burpees, 15 squats", circuit_draft(*_EXERCISES))
    client = history_route._client(factory, await _user(athlete))
    return athlete, _id(logged.session_id), client


# --- AC: a 3-round circuit of burpees and squats shows one "3 rounds" block with both -------


async def test_draft_card_shows_one_rounds_block_with_both_exercises() -> None:
    client, user_id, chat, drafts = chat_card._client()
    draft = await chat_card._pending(chat, drafts, user_id, _BURPEES_AND_SQUATS)

    card = chat_card._card(client.get(f"/drafts/{draft.id}", headers=chat_card._HTMX).text)

    (block,) = _rounds_blocks(card)
    assert block.startswith("3 rounds")
    assert "Burpees" in block
    assert "Squats" in block
    assert chat_card._text(card).index("Squats") < chat_card._text(card).index("Plank")


async def test_logged_card_shows_one_rounds_block_with_both_exercises() -> None:
    client, user_id, chat, drafts = chat_card._client()
    draft = await chat_card._pending(chat, drafts, user_id, _BURPEES_AND_SQUATS)

    logged = client.post(f"/drafts/{draft.id}/confirm", headers=chat_card._HTMX).text

    (block,) = _rounds_blocks(logged)
    assert block.startswith("3 rounds")
    assert "Burpees" in block
    assert "Squats" in block


async def test_history_list_and_session_show_one_rounds_block(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    _, session_id, client = await _logged_circuit(db_session_factory, "circuit-history")

    for page in (client.get("/history").text, client.get(f"/history/{session_id}").text):
        blocks = [block for block in _rounds_blocks(page) if "Burpees" in block]
        assert blocks
        assert all(block.startswith("3 rounds") and "Squats" in block for block in blocks)


# --- AC: changing rounds to 4 and confirming shows 4 in History ----------------------------


async def _edited_to_four_and_confirmed(
    factory: Callable[[], AsyncSession], sub: str
) -> tuple[_Athlete, UUID]:
    athlete = await _athlete(factory, sub)
    draft = await athlete.draft(
        "3 rounds: 10 burpees, 15 squats",
        _START,
        circuit_draft(*_EXERCISES),
        sport_id="gym",
    )
    await edit_draft(
        athlete.user_id,
        draft.id,
        form_changes([("activities.0.rounds", "4")]),
        drafts=athlete.drafts,
        sports=default_sport_registry(),
    )
    return athlete, _id((await athlete.confirmed(draft)).session_id)


async def test_edit_rounds_to_four_then_confirm_saves_four_rounds(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    _, session_id = await _edited_to_four_and_confirmed(db_session_factory, "circuit-four")

    assert await _circuits(db_session_factory, session_id) == [
        (1, "burpees", 1, 4, 4),
        (2, "squats", 1, 4, 4),
        (3, "plank", None, None, 1),
    ]


async def test_edit_rounds_to_four_then_confirm_shows_four_in_history(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete, session_id = await _edited_to_four_and_confirmed(
        db_session_factory, "circuit-four-history"
    )
    client = history_route._client(db_session_factory, await _user(athlete))

    # The session page shows the list beside the open session, so the circuit shows in both.
    blocks = _rounds_blocks(client.get(f"/history/{session_id}").text)

    assert len(blocks) == 2
    assert all(block.startswith("4 rounds") for block in blocks)


async def test_history_edit_form_has_one_rounds_field_per_circuit(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    _, session_id, client = await _logged_circuit(db_session_factory, "circuit-edit-form")

    form = client.get(f"/history/{session_id}/edit").text

    assert re.findall(r'name="activities\.\d+\.rounds"', form) == ['name="activities.0.rounds"']
    assert re.search(r'name="activities\.0\.rounds"[^>]*value="3"', form)


async def test_draft_edit_form_has_one_rounds_field_per_circuit() -> None:
    client, user_id, chat, drafts = chat_card._client()
    draft = await chat_card._pending(chat, drafts, user_id, _BURPEES_AND_SQUATS)

    form = client.get(f"/drafts/{draft.id}/edit", headers=chat_card._HTMX).text

    assert re.findall(r'name="activities\.\d+\.rounds"', form) == ['name="activities.0.rounds"']


async def test_history_edit_to_four_rounds_saves_four(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete, session_id, client = await _logged_circuit(db_session_factory, "circuit-edit-four")
    stored = await SqlAlchemyGymSessionReader(db_session_factory).get_session(
        athlete.user_id, session_id
    )
    assert stored is not None

    response = client.post(
        f"/history/{session_id}/edit",
        data={"version": stored.updated_at.isoformat(), "activities.0.rounds": "4"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert await _circuits(db_session_factory, session_id) == [
        (1, "burpees", 1, 4, 4),
        (2, "squats", 1, 4, 4),
        (3, "plank", None, None, 1),
    ]


# --- AC: squats' progress page counts the circuit's sets -----------------------------------


async def test_progress_counts_circuit_sets(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete, session_id, _ = await _logged_circuit(db_session_factory, "circuit-progress")
    client = progress_route._client(db_session_factory, await _user(athlete))

    page = client.get(f"/history/{session_id}/exercises/1").text

    assert "Squats" in page
    shown = " ".join(progress_route._content_text(page).split())
    assert "3 \N{MULTIPLICATION SIGN} 15" in shown, shown


# --- Edge: an entry outside any circuit, and a v6 draft, are as before ---------------------


_V6_SQUAT: dict[str, Any] = {
    "sport": "gym",
    "activities": [
        {
            "exercise_id": None,
            "keep_name": False,
            "jev_pick": False,
            "candidate_scores": {},
            "name_as_typed": "squat",
            "measure": "reps",
            "per_side": False,
            "implements": None,
            "rir": None,
            "sets": [{"reps": 10, "load": None, "duration_s": None, "distance_m": None}] * 3,
        }
    ],
}


async def test_v6_draft_confirms_unchanged() -> None:
    client, user_id, chat, drafts = chat_card._client()
    draft = await chat_card._pending(chat, drafts, user_id, _V6_SQUAT)

    (row,) = chat_card._exercises(
        chat_card._card(client.get(f"/drafts/{draft.id}", headers=chat_card._HTMX).text)
    )
    logged = chat_card._text(
        client.post(f"/drafts/{draft.id}/confirm", headers=chat_card._HTMX).text
    )

    assert chat_card._text(row) == "Squat 3 \N{MULTIPLICATION SIGN} 10"
    assert "Logged Squat to Fri 2 Oct" in logged
    assert "rounds" not in logged


async def test_plain_entry_beside_a_circuit_shows_as_before() -> None:
    client, user_id, chat, drafts = chat_card._client()
    draft = await chat_card._pending(chat, drafts, user_id, _BURPEES_AND_SQUATS)

    card = chat_card._card(client.get(f"/drafts/{draft.id}", headers=chat_card._HTMX).text)

    (block,) = _rounds_blocks(card)
    assert "Plank" not in block.split("Squats", 1)[0]
    assert "Plank 45 s" in chat_card._text(card)
