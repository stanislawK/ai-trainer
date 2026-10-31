"""Migration `b6f2a8d41c37` (ADR-0031, ADR-0024, ticket #375): the catalogue's tables, search
indexes and name normalization, created expand-only. Upgrading with data in every table is
`tests/migrations/`' job (#218)."""

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_PREVIOUS_REVISION = "e8b1f4c7a2d9"
_NEW_TABLES = {"exercises", "exercise_texts", "exercise_aliases"}


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _tables() -> set[str]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in cur.fetchall()}


def test_downgrade_drops_only_the_catalogue_and_upgrade_restores_it() -> None:
    cfg = Config("alembic.ini")
    at_head = _tables()
    try:
        command.downgrade(cfg, _PREVIOUS_REVISION)
        assert at_head - _tables() == _NEW_TABLES
        command.upgrade(cfg, "head")
        assert _tables() == at_head
    finally:
        command.upgrade(cfg, "head")


@pytest.mark.parametrize(
    ("typed", "normalized"),
    [
        ("Łydki  Ćwiczenie", "lydki cwiczenie"),
        ("  Bench\tPRESS\n", "bench press"),
        ("wyciskanie żołnierskie", "wyciskanie zolnierskie"),
    ],
)
def test_exercise_name_norm_lowers_unaccents_and_collapses_whitespace(
    typed: str, normalized: str
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT exercise_name_norm(%s)", (typed,))
        row = cur.fetchone()
    assert row == (normalized,)


def test_the_search_indexes_exist() -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE tablename IN ('exercises', 'exercise_aliases')"
        )
        indexes = dict(cur.fetchall())

    assert "gin_trgm_ops" in indexes["ix_exercises_name_trgm"]
    assert "exercise_name_norm(name)" in indexes["ix_exercises_name_trgm"]
    assert "gin_trgm_ops" in indexes["ix_exercise_aliases_alias_norm_trgm"]
    assert "exercise_name_norm(name)" in indexes["ix_exercises_name_tsv"]
    assert "alias_norm" in indexes["ix_exercise_aliases_alias_tsv"]
    assert "simple" in indexes["ix_exercises_name_tsv"]
    assert "simple" in indexes["ix_exercise_aliases_alias_tsv"]


def test_a_custom_row_must_have_an_owner() -> None:
    with _connect() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO exercises (id, sport_id, source, name, default_measure) "
                "VALUES (%s, 'gym', 'custom', 'No owner', 'reps')",
                (uuid4(),),
            )
        conn.rollback()


def test_an_alias_of_only_whitespace_is_refused() -> None:
    with _connect() as conn, conn.cursor() as cur:
        exercise = uuid4()
        cur.execute(
            "INSERT INTO exercises (id, sport_id, source, source_ref, name, default_measure) "
            "VALUES (%s, 'gym', 'dataset', 'zz-blank-alias', 'Row', 'reps')",
            (exercise,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO exercise_aliases (id, exercise_id, alias) VALUES (%s, %s, '   ')",
                (uuid4(), exercise),
            )
        conn.rollback()


def test_default_measure_is_a_closed_set() -> None:
    with _connect() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO exercises (id, sport_id, source, source_ref, name, default_measure) "
                "VALUES (%s, 'gym', 'dataset', 'zz-measure', 'Bad', 'furlongs')",
                (uuid4(),),
            )
        conn.rollback()
