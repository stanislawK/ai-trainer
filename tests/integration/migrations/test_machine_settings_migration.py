"""Migrations `a3f8c2e6d914` and `c71d4b9e2a58` (ADR-0032 #618 amendment, ADR-0024, #630): a set
gains its machine's incline, level and watts as nullable columns, expand only. Every set seeded
before reads NULL in them with its other values kept, and the CHECKs, added NOT VALID and then
validated, hold ADR-0032's bounds."""

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

_BEFORE = "5e9a2c7b4f18"
# Seeded before the upgrade, as (reps, load_kg, load_original, duration_s, distance_m, band_label).
_SETS: list[tuple[Any, ...]] = [
    (8, Decimal("60.00"), "60 kg", None, None, None),
    (None, None, None, 1200, None, None),
    (None, None, None, 470, Decimal("2000.0"), None),
    (12, None, None, None, None, "red"),
    (None, None, None, None, None, None),
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
        "payload_version, created_at, updated_at) VALUES (%s, %s, 'gym', '2026-11-24', "
        "'Europe/Warsaw', '{}', 6, now(), now())",
        (session_id, user_id),
    )
    cur.execute(
        "INSERT INTO gym_exercise_entries (id, user_id, session_id, position, name_as_typed, "
        "measure) VALUES (%s, %s, %s, 1, 'treadmill', 'time')",
        (entry_id, user_id, session_id),
    )
    for set_no, values in enumerate(_SETS, start=1):
        cur.execute(
            "INSERT INTO gym_sets (id, user_id, entry_id, set_no, reps, load_kg, load_original, "
            "duration_s, distance_m, band_label) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (uuid4(), user_id, entry_id, set_no, *values),
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


def test_columns_are_added_nullable_and_existing_sets_read_null(migrated: UUID) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, numeric_precision, numeric_scale, is_nullable, "
            "column_default FROM information_schema.columns WHERE table_name = 'gym_sets' AND "
            "column_name IN ('incline_pct', 'machine_level', 'watts') ORDER BY column_name"
        )
        assert cur.fetchall() == [
            ("incline_pct", "numeric", 4, 1, "YES", None),
            ("machine_level", "smallint", 16, 0, "YES", None),
            ("watts", "smallint", 16, 0, "YES", None),
        ]
        cur.execute(
            "SELECT incline_pct, machine_level, watts FROM gym_sets WHERE entry_id = %s",
            (migrated,),
        )
        assert cur.fetchall() == [(None, None, None)] * len(_SETS)


def test_upgrade_with_data_keeps_every_seeded_set(migrated: UUID) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT reps, load_kg, load_original, duration_s, distance_m, band_label FROM gym_sets "
            "WHERE entry_id = %s ORDER BY set_no",
            (migrated,),
        )
        assert cur.fetchall() == _SETS


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("incline_pct", Decimal("40.1")),
        ("incline_pct", Decimal("-10.1")),
        ("machine_level", 0),
        ("machine_level", 51),
        ("watts", 0),
        ("watts", 2001),
    ],
)
def test_checks_are_validated_and_refuse_out_of_range_values(
    migrated: UUID, column: str, value: Decimal | int
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT conname, convalidated FROM pg_constraint WHERE conname IN "
            "('gym_sets_incline_pct', 'gym_sets_machine_level', 'gym_sets_watts') ORDER BY conname"
        )
        assert cur.fetchall() == [
            ("gym_sets_incline_pct", True),
            ("gym_sets_machine_level", True),
            ("gym_sets_watts", True),
        ]
        with pytest.raises(psycopg.errors.CheckViolation, match=f"gym_sets_{column}"):
            cur.execute(
                f"UPDATE gym_sets SET {column} = %s WHERE entry_id = %s AND set_no = 1",
                (value, migrated),
            )
        conn.rollback()


@pytest.mark.parametrize(
    ("column", "low", "high"),
    [
        ("incline_pct", Decimal("-10.0"), Decimal("40.0")),
        ("machine_level", 1, 50),
        ("watts", 1, 2000),
    ],
)
def test_checks_accept_their_bounds(
    migrated: UUID, column: str, low: Decimal | int, high: Decimal | int
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        for value in (low, high):
            cur.execute(
                f"UPDATE gym_sets SET {column} = %s WHERE entry_id = %s AND set_no = 2 "
                f"RETURNING {column}",
                (value, migrated),
            )
            assert cur.fetchone() == (value,)
        conn.rollback()
