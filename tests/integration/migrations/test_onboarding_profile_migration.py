"""Migration `c894aa4b2c20` (ADR-0028, ADR-0024, ticket #254): `athlete_profiles` and
`onboarding_answers`, created expand-only, so upgrading from `main`'s head keeps every existing
row as it was; both join account deletion."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_PREVIOUS_REVISION = "d3a7e5c1b942"
_TABLES = ("athlete_profiles", "onboarding_answers")
# Every table already there before this migration, and what identifies a row in it.
_EXISTING = {
    "users": "id",
    "conversations": "id",
    "chat_messages": "id",
    "sessions": "id",
    "user_sports": "user_id, sport_id",
    "weekly_availability": "user_id, weekday",
    "goals": "id",
    "llm_calls": "id",
    "user_status_changes": "id",
    "training_sessions": "id",
}


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _seed_user(cur: psycopg.Cursor[Any]) -> UUID:
    user, conversation = uuid4(), uuid4()
    cur.execute(
        "INSERT INTO users (id, sub, email) VALUES (%s, %s, %s)",
        (user, f"profile-{user}", f"{user}@example.com"),
    )
    cur.execute(
        "INSERT INTO conversations (id, user_id, title, last_message_at)"
        " VALUES (%s, %s, 'Climbed', now())",
        (conversation, user),
    )
    cur.execute(
        "INSERT INTO chat_messages (id, user_id, conversation_id, role, text)"
        " VALUES (%s, %s, %s, 'user', 'I climbed')",
        (uuid4(), user, conversation),
    )
    cur.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (%s, %s, now())", (uuid4(), user)
    )
    cur.execute("INSERT INTO user_sports (user_id, sport_id) VALUES (%s, 'climbing')", (user,))
    cur.execute(
        "INSERT INTO weekly_availability (user_id, weekday, minutes) VALUES (%s, 1, 90)", (user,)
    )
    cur.execute(
        "INSERT INTO goals (id, user_id, position, text) VALUES (%s, %s, 0, 'Send 7a')",
        (uuid4(), user),
    )
    cur.execute(
        "INSERT INTO llm_calls (id, user_id, template_id, template_version, model,"
        " input_tokens, output_tokens, latency_ms, outcome)"
        " VALUES (%s, %s, 'router', 2, 'm', 1, 1, 1, 'success')",
        (uuid4(), user),
    )
    cur.execute(
        "INSERT INTO user_status_changes (id, actor_user_id, target_user_id, old_status,"
        " new_status) VALUES (%s, %s, %s, 'pending', 'active')",
        (uuid4(), user, user),
    )
    cur.execute(
        "INSERT INTO training_sessions (id, user_id, sport_id, occurred_on, timezone, payload,"
        " payload_version, created_at, updated_at)"
        " VALUES (%s, %s, 'climbing', '2026-10-20', 'UTC', '{}', 1, now(), now())",
        (uuid4(), user),
    )
    return user


def _snapshot(cur: psycopg.Cursor[Any]) -> dict[str, list[tuple[Any, ...]]]:
    snapshot = {}
    for table, key in _EXISTING.items():
        cur.execute(f"SELECT * FROM {table} ORDER BY {key}")
        snapshot[table] = cur.fetchall()
    return snapshot


def _existing_tables() -> set[str]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in cur.fetchall()}


@pytest.fixture
def at_previous_revision() -> Iterator[None]:
    """`main`'s head with two athletes' rows in every table; leaves the database at head."""
    cfg = Config("alembic.ini")
    command.downgrade(cfg, _PREVIOUS_REVISION)
    try:
        with _connect() as conn, conn.cursor() as cur:
            _seed_user(cur)
            _seed_user(cur)
        yield
    finally:
        command.upgrade(cfg, "head")


def test_upgrading_with_data_in_every_table_keeps_every_row_and_value(
    at_previous_revision: None,
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        before = _snapshot(cur)

    command.upgrade(Config("alembic.ini"), "head")

    with _connect() as conn, conn.cursor() as cur:
        assert _snapshot(cur) == before
    assert all(before[table] for table in _EXISTING)


def test_downgrade_drops_only_the_two_tables_and_upgrade_restores_them() -> None:
    cfg = Config("alembic.ini")
    try:
        command.downgrade(cfg, _PREVIOUS_REVISION)
        assert _existing_tables() & set(_TABLES) == set()
        assert _existing_tables() >= set(_EXISTING)
        command.upgrade(cfg, "head")
        assert _existing_tables() >= set(_TABLES)
    finally:
        command.upgrade(cfg, "head")


@pytest.fixture
def athlete() -> Iterator[UUID]:
    with _connect() as conn, conn.cursor() as cur:
        user = uuid4()
        cur.execute(
            "INSERT INTO users (id, sub, email) VALUES (%s, %s, %s)",
            (user, f"profile-{user}", f"{user}@example.com"),
        )
    yield user
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user,))


def _answer(cur: psycopg.Cursor[Any], user: UUID, step: str, sport: str | None) -> None:
    cur.execute(
        "INSERT INTO onboarding_answers (id, user_id, step, sport_id, revision, status)"
        " VALUES (%s, %s, %s, %s, 1, 'pending')",
        (uuid4(), user, step, sport),
    )


def test_about_you_is_one_row_per_athlete_although_its_sport_is_null(athlete: UUID) -> None:
    with _connect() as conn, conn.cursor() as cur:
        _answer(cur, athlete, "about_you", None)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _answer(cur, athlete, "about_you", None)


@pytest.mark.parametrize(
    ("step", "sport"), [("about_you", "climbing"), ("goals", None), ("intro", None)]
)
def test_a_step_and_its_sport_must_go_together(athlete: UUID, step: str, sport: str | None) -> None:
    with _connect() as conn, conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        _answer(cur, athlete, step, sport)


@pytest.mark.parametrize(
    "values",
    [
        "height_cm => 99",
        "height_cm => 251",
        "weight_kg => 24.9",
        "weight_kg => 300.1",
        "birth_year => 1899",
        "limits => repeat('x', 301)",
    ],
)
def test_athlete_profiles_rejects_values_outside_its_checks(athlete: UUID, values: str) -> None:
    column, value = values.split(" => ")
    with _connect() as conn, conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            f"INSERT INTO athlete_profiles (user_id, {column}) VALUES (%s, {value})",
            (athlete,),
        )


def test_deleting_the_athlete_deletes_their_profile_and_answers(athlete: UUID) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO athlete_profiles (user_id, height_cm) VALUES (%s, 178)", (athlete,)
        )
        _answer(cur, athlete, "about_you", None)
        cur.execute("DELETE FROM users WHERE id = %s", (athlete,))
        cur.execute(
            "SELECT (SELECT count(*) FROM athlete_profiles WHERE user_id = %s)"
            " + (SELECT count(*) FROM onboarding_answers WHERE user_id = %s)",
            (athlete, athlete),
        )
        assert cur.fetchone() == (0,)
