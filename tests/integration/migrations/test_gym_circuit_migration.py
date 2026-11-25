"""Migrations `a4475577b9b1` and `d076e3a63f7b` (ADR-0032 #618 amendment, ADR-0024, #632): an
entry gains its circuit and rounds as nullable columns, expand only. Every entry seeded before
reads NULL in both with its other values and sets kept, and the CHECKs, added NOT VALID and then
validated, refuse a half-set pair and out-of-range values."""

from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_BEFORE = "c71d4b9e2a58"
_CHECKS = [
    ("gym_exercise_entries_circuit_range", True),
    ("gym_exercise_entries_circuit_rounds", True),
    ("gym_exercise_entries_rounds_range", True),
]


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _seed(cur: psycopg.Cursor[Any], user_id: UUID) -> UUID:
    session_id, entry_id = uuid4(), uuid4()
    cur.execute(
        "INSERT INTO users (id, sub, email) VALUES (%s, %s, %s)",
        (user_id, str(user_id), f"{user_id}@example.com"),
    )
    cur.execute(
        "INSERT INTO training_sessions (id, user_id, sport_id, occurred_on, timezone, payload, "
        "payload_version, created_at, updated_at) VALUES (%s, %s, 'gym', '2026-11-25', "
        "'Europe/Warsaw', '{}', 7, now(), now())",
        (session_id, user_id),
    )
    cur.execute(
        "INSERT INTO gym_exercise_entries (id, user_id, session_id, position, name_as_typed, "
        "measure, rir) VALUES (%s, %s, %s, 1, 'squat', 'load_reps', 2)",
        (entry_id, user_id, session_id),
    )
    cur.execute(
        "INSERT INTO gym_sets (id, user_id, entry_id, set_no, reps, load_kg, load_original) "
        "VALUES (%s, %s, %s, 1, 8, 60, '60 kg')",
        (uuid4(), user_id, entry_id),
    )
    return entry_id


@pytest.fixture(scope="module")
def migrated() -> Iterator[UUID]:
    cfg = Config("alembic.ini")
    try:
        command.downgrade(cfg, _BEFORE)
        with _connect() as conn, conn.cursor() as cur:
            entry_id = _seed(cur, uuid4())
            conn.commit()
        command.upgrade(cfg, "head")
        yield entry_id
    finally:
        command.upgrade(cfg, "head")


def test_columns_are_added_nullable_and_existing_entries_read_null(migrated: UUID) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable, column_default FROM "
            "information_schema.columns WHERE table_name = 'gym_exercise_entries' AND "
            "column_name IN ('circuit', 'rounds') ORDER BY column_name"
        )
        assert cur.fetchall() == [
            ("circuit", "smallint", "YES", None),
            ("rounds", "smallint", "YES", None),
        ]
        cur.execute(
            "SELECT name_as_typed, measure, rir, circuit, rounds FROM gym_exercise_entries "
            "WHERE id = %s",
            (migrated,),
        )
        assert cur.fetchall() == [("squat", "load_reps", 2, None, None)]
        cur.execute(
            "SELECT set_no, reps, load_kg, load_original FROM gym_sets WHERE entry_id = %s",
            (migrated,),
        )
        assert cur.fetchall() == [(1, 8, Decimal("60.00"), "60 kg")]


@pytest.mark.parametrize(("circuit", "rounds"), [(1, None), (None, 3)])
def test_circuit_checks_reject_half_set_pair(
    migrated: UUID, circuit: int | None, rounds: int | None
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT conname, convalidated FROM pg_constraint WHERE conname LIKE "
            "'gym_exercise_entries_%%circuit%%' OR conname = 'gym_exercise_entries_rounds_range' "
            "ORDER BY conname"
        )
        assert cur.fetchall() == _CHECKS
        with pytest.raises(
            psycopg.errors.CheckViolation, match="gym_exercise_entries_circuit_rounds"
        ):
            cur.execute(
                "UPDATE gym_exercise_entries SET circuit = %s, rounds = %s WHERE id = %s",
                (circuit, rounds, migrated),
            )
        conn.rollback()


@pytest.mark.parametrize(
    ("circuit", "rounds", "check"),
    [
        (0, 3, "gym_exercise_entries_circuit_range"),
        (101, 3, "gym_exercise_entries_circuit_range"),
        (1, 0, "gym_exercise_entries_rounds_range"),
        (1, 101, "gym_exercise_entries_rounds_range"),
    ],
)
def test_checks_refuse_out_of_range_values(
    migrated: UUID, circuit: int, rounds: int, check: str
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation, match=check):
            cur.execute(
                "UPDATE gym_exercise_entries SET circuit = %s, rounds = %s WHERE id = %s",
                (circuit, rounds, migrated),
            )
        conn.rollback()


def test_checks_accept_a_circuit_at_its_bounds(migrated: UUID) -> None:
    with _connect() as conn, conn.cursor() as cur:
        for value in (1, 100):
            cur.execute(
                "UPDATE gym_exercise_entries SET circuit = %s, rounds = %s WHERE id = %s "
                "RETURNING circuit, rounds",
                (value, value, migrated),
            )
            assert cur.fetchone() == (value, value)
        conn.rollback()
