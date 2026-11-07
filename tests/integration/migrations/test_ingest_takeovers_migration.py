"""Migration `a4c19e7b5d20` (ADR-0036's #462 amendment, ADR-0024): `knowledge_sources` gains
`ingest_takeovers`, how many times a worker took the source over from one that stopped. Upgrading
with data in every table is `tests/migrations/`' job (#218)."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_PREVIOUS_REVISION = "2296d6928690"
_AT = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _insert_source(conn: psycopg.Connection[Any]) -> UUID:
    source_id = uuid4()
    conn.execute(
        "INSERT INTO knowledge_sources (id, title, source_type, evidence_tier, language, sports,"
        " content_hash, uploaded_at, license_id, license_source, status)"
        " VALUES (%s, 'Squat depth', 'article', 'article_or_transcript', 'en', '{gym}', %s, %s,"
        " 'cc-by', 'admin', 'ingesting')",
        (source_id, uuid4().hex * 2, _AT),
    )
    return source_id


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    with _connect() as connection:
        yield connection
        connection.rollback()


def test_a_new_source_starts_with_no_takeovers(conn: psycopg.Connection[Any]) -> None:
    source_id = _insert_source(conn)
    row = conn.execute(
        "SELECT ingest_takeovers FROM knowledge_sources WHERE id = %s", (source_id,)
    ).fetchone()
    assert row == (0,)


def test_the_migration_keeps_an_existing_source_with_no_takeovers() -> None:
    cfg = Config("alembic.ini")
    command.downgrade(cfg, _PREVIOUS_REVISION)
    try:
        with _connect() as conn:
            source_id = _insert_source(conn)
            conn.commit()
        command.upgrade(cfg, "head")
        with _connect() as conn:
            row = conn.execute(
                "SELECT status, ingest_takeovers FROM knowledge_sources WHERE id = %s",
                (source_id,),
            ).fetchone()
    finally:
        command.upgrade(cfg, "head")
        with _connect() as conn:
            conn.execute("DELETE FROM knowledge_sources WHERE id = %s", (source_id,))
            conn.commit()
    assert row == ("ingesting", 0)
