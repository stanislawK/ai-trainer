"""Migrations `3c7e1a9f5d24`, `8b2f6d4e1a73` and `5e9a2c7b4f18` (ADR-0032 #618 amendment,
ADR-0024, #626): the measure CHECKs admit `distance_time`, expand only, and a gym entry whose
first set with a value holds a distance and a time moves to it, its sets unchanged. A carry with
only a distance, a plank with only a time, and another user's rows are read on their own."""

import importlib.util
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_BEFORE = "e5b9f3c7a2d0"
_MOVE = Path("migrations/versions/5e9a2c7b4f18_move_timed_distances_to_distance_time.py")

# (name, measure, sets as (reps, duration_s, distance_m)), seeded before the upgrade.
_ENTRIES: dict[str, tuple[str, list[tuple[int | None, int | None, Decimal | None]]]] = {
    "rower": ("time", [(None, 470, Decimal("2000.0"))]),
    "run": ("distance", [(None, 1500, Decimal("5000.0"))]),
    # The first set with a value decides: a blank first set is skipped.
    "erg after a blank": ("time", [(None, None, None), (None, 240, Decimal("1000.0"))]),
    "carry": ("distance", [(None, None, Decimal("40.0"))]),
    "plank": ("time", [(None, 45, None), (None, 45, None)]),
    # The first valued set has only a time, so a later timed distance doesn't move it.
    "intervals": ("time", [(None, 60, None), (None, 240, Decimal("1000.0"))]),
    # Measured by load, so not moved even with both values.
    "sled": ("load_reps", [(5, 30, Decimal("20.0"))]),
}
_MOVED = {"rower", "run", "erg after a blank"}


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _seed(cur: psycopg.Cursor[Any], user_id: UUID) -> dict[str, UUID]:
    session_id = uuid4()
    cur.execute(
        "INSERT INTO users (id, sub, email) VALUES (%s, %s, %s)",
        (user_id, str(user_id), f"{user_id}@example.com"),
    )
    cur.execute(
        "INSERT INTO training_sessions (id, user_id, sport_id, occurred_on, timezone, payload, "
        "payload_version, created_at, updated_at) VALUES (%s, %s, 'gym', '2026-11-19', "
        "'Europe/Warsaw', '{}', 2, now(), now())",
        (session_id, user_id),
    )
    entries: dict[str, UUID] = {}
    for position, (name, (measure, sets)) in enumerate(_ENTRIES.items(), start=1):
        entry_id = entries[name] = uuid4()
        cur.execute(
            "INSERT INTO gym_exercise_entries (id, user_id, session_id, position, name_as_typed, "
            "measure) VALUES (%s, %s, %s, %s, %s, %s)",
            (entry_id, user_id, session_id, position, name, measure),
        )
        for set_no, (reps, seconds, metres) in enumerate(sets, start=1):
            cur.execute(
                "INSERT INTO gym_sets (id, user_id, entry_id, set_no, reps, duration_s, "
                "distance_m) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (uuid4(), user_id, entry_id, set_no, reps, seconds, metres),
            )
    return entries


@pytest.fixture(scope="module")
def migrated() -> Iterator[dict[str, UUID]]:
    cfg = Config("alembic.ini")
    user_id = uuid4()
    try:
        command.downgrade(cfg, _BEFORE)
        with _connect() as conn, conn.cursor() as cur:
            entries = _seed(cur, user_id)
            conn.commit()
            # Before the widening the measure is refused.
            with pytest.raises(psycopg.errors.CheckViolation, match="gym_exercise_entries"):
                cur.execute(
                    "UPDATE gym_exercise_entries SET measure = 'distance_time' WHERE id = %s",
                    (entries["carry"],),
                )
            conn.rollback()
        command.upgrade(cfg, "head")
        yield entries
    finally:
        command.upgrade(cfg, "head")


def _measures(entries: dict[str, UUID]) -> dict[str, str]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name_as_typed, measure FROM gym_exercise_entries WHERE id = ANY(%s)",
            (list(entries.values()),),
        )
        return dict(cur.fetchall())


def test_entries_with_distance_and_time_move_to_distance_time(
    migrated: dict[str, UUID],
) -> None:
    measures = _measures(migrated)

    assert {name for name, measure in measures.items() if measure == "distance_time"} == _MOVED
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT reps, duration_s, distance_m FROM gym_sets WHERE entry_id = %s ORDER BY set_no",
            (migrated["erg after a blank"],),
        )
        assert cur.fetchall() == _ENTRIES["erg after a blank"][1]
        cur.execute(
            "SELECT duration_s, distance_m FROM gym_sets WHERE entry_id = %s",
            (migrated["rower"],),
        )
        assert cur.fetchall() == [(470, Decimal("2000.0"))]


def test_distance_only_and_time_only_entries_keep_their_measure(
    migrated: dict[str, UUID],
) -> None:
    measures = _measures(migrated)

    assert measures["carry"] == "distance"
    assert measures["plank"] == "time"
    assert measures["intervals"] == "time"
    assert measures["sled"] == "load_reps"


def test_the_wider_checks_are_validated_and_still_refuse_other_values(
    migrated: dict[str, UUID],
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT conname, convalidated FROM pg_constraint WHERE conname IN "
            "('gym_exercise_entries_measure', 'gym_exercise_entries_measure_v2', "
            "'exercises_default_measure', 'exercises_default_measure_v2') ORDER BY conname"
        )
        assert cur.fetchall() == [
            ("exercises_default_measure_v2", True),
            ("gym_exercise_entries_measure_v2", True),
        ]
        cur.execute(
            "INSERT INTO exercises (id, sport_id, owner_user_id, source, name, default_measure) "
            "SELECT gen_random_uuid(), 'gym', user_id, 'custom', 'rower', 'distance_time' "
            "FROM gym_exercise_entries WHERE id = %s",
            (migrated["rower"],),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="measure_v2"):
            cur.execute(
                "UPDATE gym_exercise_entries SET measure = 'pace' WHERE id = %s",
                (migrated["rower"],),
            )
        conn.rollback()


def test_a_second_run_of_the_move_changes_nothing(migrated: dict[str, UUID]) -> None:
    spec = importlib.util.spec_from_file_location("move_timed_distances", _MOVE)
    assert spec is not None
    assert spec.loader is not None
    moved = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(moved)
    before = _measures(migrated)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE gym_exercise_entries AS t SET measure = 'distance_time' WHERE {moved.MOVED}"
        )
        conn.commit()

    assert _measures(migrated) == before
