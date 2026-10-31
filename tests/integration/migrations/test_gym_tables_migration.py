"""Migration `a47987a22760` (ADR-0032, ADR-0024, ticket #376): the gym's tables, created
expand-only, with every key's `ON DELETE` and every CHECK the ADR states. Upgrading with data in
every table is `tests/migrations/`' job (#218)."""

from typing import Any

import psycopg
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_REVISION = "a47987a22760"
_PREVIOUS_REVISION = "b6f2a8d41c37"
_NEW_TABLES = {"gym_exercise_entries", "gym_sets", "athlete_bands"}


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _tables() -> set[str]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in cur.fetchall()}


def _constraints(kind: str) -> dict[str, str]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE contype = %s AND conrelid::regclass::text = ANY(%s)",
            (kind, sorted(_NEW_TABLES)),
        )
        return dict(cur.fetchall())


def test_downgrade_drops_only_the_gym_tables_and_upgrade_restores_them() -> None:
    cfg = Config("alembic.ini")
    try:
        # From this revision, so a later one's tables don't count.
        command.downgrade(cfg, _REVISION)
        at_revision = _tables()
        command.downgrade(cfg, _PREVIOUS_REVISION)
        assert at_revision - _tables() == _NEW_TABLES
        command.upgrade(cfg, _REVISION)
        assert _tables() == at_revision
    finally:
        command.upgrade(cfg, "head")


def test_every_reference_is_keyed_with_user_id_and_has_its_on_delete() -> None:
    keys = _constraints("f")

    assert keys["gym_exercise_entries_session_id_user_id_fkey"] == (
        "FOREIGN KEY (session_id, user_id) REFERENCES training_sessions(id, user_id)"
        " ON DELETE CASCADE"
    )
    assert keys["gym_exercise_entries_source_id_user_id_fkey"] == (
        "FOREIGN KEY (source_id, user_id) REFERENCES training_session_sources(id, user_id)"
        " ON DELETE CASCADE"
    )
    assert keys["gym_sets_entry_id_user_id_fkey"] == (
        "FOREIGN KEY (entry_id, user_id) REFERENCES gym_exercise_entries(id, user_id)"
        " ON DELETE CASCADE"
    )
    assert keys["gym_sets_band_id_user_id_fkey"] == (
        "FOREIGN KEY (band_id, user_id) REFERENCES athlete_bands(id, user_id)"
        " ON DELETE SET NULL (band_id)"
    )
    assert keys["gym_exercise_entries_exercise_id_fkey"] == (
        "FOREIGN KEY (exercise_id) REFERENCES exercises(id)"
    )
    for table in _NEW_TABLES:
        assert keys[f"{table}_user_id_fkey"] == (
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
        )


def test_every_check_of_the_adr_is_present() -> None:
    checks = _constraints("c")

    assert (
        "'load_reps'::text, 'reps'::text, 'time'::text, 'distance'::text, 'band_reps'"
        in (checks["gym_exercise_entries_measure"])
    )
    assert "implements >= 1" in checks["gym_exercise_entries_implements_range"]
    assert "implements <= 4" in checks["gym_exercise_entries_implements_range"]
    assert "rir >= 0" in checks["gym_exercise_entries_rir_range"]
    assert "rir <= 10" in checks["gym_exercise_entries_rir_range"]
    assert "<= 500" in checks["gym_exercise_entries_note_length"]


def test_user_id_is_indexed_on_every_gym_table() -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tablename, indexdef FROM pg_indexes WHERE tablename = ANY(%s)",
            (sorted(_NEW_TABLES),),
        )
        leading = {
            table for table, definition in cur.fetchall() if "USING btree (user_id" in definition
        }
    assert leading == _NEW_TABLES
