"""Migration `7d2c9e4a1f63` (ADR-0028, ADR-0024, ticket #255): `sport_profiles`, created
expand-only, bounded by its checks, and joining account deletion. Upgrading with data in every
table is `tests/migrations/`' job (#218)."""

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_THIS_REVISION = "7d2c9e4a1f63"
_PREVIOUS_REVISION = "c894aa4b2c20"


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _tables() -> set[str]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in cur.fetchall()}


def test_downgrade_drops_only_sport_profiles_and_upgrade_restores_it() -> None:
    cfg = Config("alembic.ini")
    at_head = _tables()
    try:
        # Later revisions add tables of their own, so compare from this revision.
        command.downgrade(cfg, _THIS_REVISION)
        at_this_revision = _tables()
        command.downgrade(cfg, _PREVIOUS_REVISION)
        assert at_this_revision - _tables() == {"sport_profiles"}
        command.upgrade(cfg, "head")
        assert _tables() == at_head
    finally:
        command.upgrade(cfg, "head")


@pytest.fixture
def athlete() -> Iterator[UUID]:
    with _connect() as conn, conn.cursor() as cur:
        user = uuid4()
        cur.execute(
            "INSERT INTO users (id, sub, email) VALUES (%s, %s, %s)",
            (user, f"sport-{user}", f"{user}@example.com"),
        )
    yield user
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (user,))


def test_a_full_row_is_stored_with_its_days(athlete: UUID) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sport_profiles (user_id, sport_id, level, exercises, sessions_per_week,"
            " session_minutes, preferred_days)"
            " VALUES (%s, 'climbing', '7b', 'Board', 4, 120, ARRAY[0, 2, 4, 5])",
            (athlete,),
        )
        cur.execute("SELECT preferred_days FROM sport_profiles WHERE user_id = %s", (athlete,))
        assert cur.fetchone() == ([0, 2, 4, 5],)


def test_one_row_per_athlete_and_sport(athlete: UUID) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO sport_profiles (user_id, sport_id) VALUES (%s, 'gym')", (athlete,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO sport_profiles (user_id, sport_id) VALUES (%s, 'gym')", (athlete,)
            )


@pytest.mark.parametrize(
    "values",
    [
        "sessions_per_week => 0",
        "sessions_per_week => 15",
        "session_minutes => 4",
        "session_minutes => 601",
        "preferred_days => ARRAY[7]",
        "preferred_days => ARRAY[-1]",
        "level => repeat('x', 201)",
        "exercises => repeat('x', 301)",
    ],
)
def test_sport_profiles_rejects_values_outside_its_checks(athlete: UUID, values: str) -> None:
    column, value = values.split(" => ")
    with _connect() as conn, conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            f"INSERT INTO sport_profiles (user_id, sport_id, {column})"
            f" VALUES (%s, 'climbing', {value})",
            (athlete,),
        )


def test_deleting_the_athlete_deletes_their_sport_profiles(athlete: UUID) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sport_profiles (user_id, sport_id) VALUES (%s, 'climbing')", (athlete,)
        )
        cur.execute("DELETE FROM users WHERE id = %s", (athlete,))
        cur.execute("SELECT count(*) FROM sport_profiles WHERE user_id = %s", (athlete,))
        assert cur.fetchone() == (0,)
