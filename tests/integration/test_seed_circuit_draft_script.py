"""`scripts/seed_circuit_draft.py` (ticket #632): the demo's pending circuit draft, in a new
conversation of the seeded dev user. Like `test_dev_session_script`, it commits real rows on its
own engine, so it runs in the `db_schema` group and deletes its user afterwards (everything
cascades from `users.id`)."""

import io
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.sports.gym.payload import GYM_PAYLOAD_VERSION, GymPayload
from ai_trainer.settings import Settings
from scripts.dev_session import DEV_SESSION_SUB, get_or_create_active_user
from scripts.seed_circuit_draft import LOG, main, parse_args

_SUB = "seed-circuit-draft-test"


@pytest.fixture
async def _cleanup() -> AsyncIterator[None]:
    yield
    engine = build_engine(str(Settings().database_url))
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM users WHERE sub = :sub"), {"sub": _SUB})
    await engine.dispose()


@pytest.mark.usefixtures("_cleanup")
async def test_main_seeds_a_pending_draft_with_a_three_round_circuit() -> None:
    engine = build_engine(str(Settings().database_url))
    try:
        await get_or_create_active_user(
            SqlAlchemyUsersRepository(build_session_factory(engine)),
            sub=_SUB,
            email=f"{_SUB}@example.com",
        )
    finally:
        await engine.dispose()
    output = io.StringIO()

    draft_id = await main(output=output, sub=_SUB)

    engine = build_engine(str(Settings().database_url))
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT d.status, d.sport_id, d.payload_version, d.draft, m.text"
                    " FROM session_drafts d JOIN chat_messages m ON m.id = d.message_id"
                    " WHERE d.id = :id"
                ),
                {"id": draft_id},
            )
        ).one()
    await engine.dispose()
    status, sport_id, version, draft, log = row
    assert (status, sport_id, version, log) == ("pending", "gym", GYM_PAYLOAD_VERSION, LOG)
    payload = GymPayload.model_validate(draft["payload"])
    assert [(e.name_as_typed, e.circuit, e.rounds) for e in payload.activities] == [
        ("burpees", 1, 3),
        ("squats", 1, 3),
        ("plank", None, None),
    ]
    assert f"circuit draft {draft_id}" in output.getvalue()


async def test_main_without_the_seeded_user_writes_nothing() -> None:
    with pytest.raises(LookupError, match="dev_session"):
        await main(output=io.StringIO(), sub="seed-circuit-draft-nobody")


def test_parse_args_defaults_to_the_dev_session_user() -> None:
    assert parse_args([]).sub == DEV_SESSION_SUB
    assert parse_args(["--sub", "x"]).sub == "x"
