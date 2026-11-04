"""Migration `fe77f6d38ca0` (ADR-0036, ADR-0024, #423): `knowledge_chunks` with its HNSW and GIN
indexes, and the worker's claim columns on `knowledge_sources`. Upgrading with data in every
table is `tests/migrations/`' job (#218)."""

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

_PREVIOUS_REVISION = "c3a85f0d6e17"
_AT = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
_VECTOR = "[" + ",".join(["0.01"] * 1024) + "]"


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _insert_source(conn: psycopg.Connection[Any]) -> UUID:
    source_id = uuid4()
    conn.execute(
        "INSERT INTO knowledge_sources (id, title, source_type, evidence_tier, language, sports,"
        " content_hash, uploaded_at, license_id, license_source, status)"
        " VALUES (%s, 'Squat depth', 'article', 'article_or_transcript', 'en', '{gym}', %s, %s,"
        " 'cc-by', 'admin', 'uploaded')",
        (source_id, uuid4().hex * 2, _AT),
    )
    return source_id


def _insert_chunk(conn: psycopg.Connection[Any], source_id: UUID, **columns: object) -> None:
    values: dict[str, object] = {
        "id": uuid4(),
        "source_id": source_id,
        "ordinal": 0,
        "text": "Squat to a depth you control.",
        "embedding": _VECTOR,
        "embedding_model": "qwen/qwen3-embedding-8b",
        "sports": ["gym"],
        "language": "en",
        "section_kind": "body",
        "locator": '{"kind": "section", "section": null}',
        "search_vector": "'squat':1",
        "created_at": _AT,
        **columns,
    }
    names = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    conn.execute(
        f"INSERT INTO knowledge_chunks ({names}) VALUES ({placeholders})", list(values.values())
    )


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    """A connection whose writes are rolled back, so a refused insert leaves nothing behind."""
    with _connect() as connection:
        yield connection
        connection.rollback()


def test_the_chunk_indexes_are_hnsw_on_cosine_and_gin_on_the_tsvector(
    conn: psycopg.Connection[Any],
) -> None:
    rows = conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'knowledge_chunks'"
    ).fetchall()
    definitions = dict(rows)
    assert (
        "USING hnsw (embedding vector_cosine_ops)" in definitions["ix_knowledge_chunks_embedding"]
    )
    assert "USING gin (search_vector)" in definitions["ix_knowledge_chunks_search_vector"]
    assert "WHERE (exercise_id IS NOT NULL)" in definitions["ix_knowledge_chunks_exercise_id"]


def test_a_source_has_one_chunk_per_ordinal(conn: psycopg.Connection[Any]) -> None:
    source_id = _insert_source(conn)
    _insert_chunk(conn, source_id)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_chunk(conn, source_id)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("section_kind", "methods"),
        ("locator", '{"kind": "chapter"}'),
        ("language", "de"),
        ("sports", []),
        ("text", "   "),
    ],
    ids=["section_kind", "locator_kind", "language", "sports", "blank_text"],
)
def test_a_chunk_outside_the_closed_sets_is_refused(
    conn: psycopg.Connection[Any], column: str, value: object
) -> None:
    source_id = _insert_source(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_chunk(conn, source_id, **{column: value})


def test_deleting_a_source_deletes_its_chunks(conn: psycopg.Connection[Any]) -> None:
    source_id = _insert_source(conn)
    _insert_chunk(conn, source_id)
    conn.execute("DELETE FROM knowledge_sources WHERE id = %s", (source_id,))
    count = conn.execute(
        "SELECT count(*) FROM knowledge_chunks WHERE source_id = %s", (source_id,)
    ).fetchone()
    assert count == (0,)


def test_the_migration_downgrades_and_upgrades_again_keeping_sources() -> None:
    cfg = Config("alembic.ini")
    with _connect() as conn:
        source_id = _insert_source(conn)
        conn.commit()
    try:
        command.downgrade(cfg, _PREVIOUS_REVISION)
        with _connect() as conn:
            gone = conn.execute("SELECT to_regclass('knowledge_chunks')").fetchone()
        command.upgrade(cfg, "head")
        with _connect() as conn:
            claim = conn.execute(
                "SELECT claim_id, lease_expires_at, chunk_count, embedding_model"
                " FROM knowledge_sources WHERE id = %s",
                (source_id,),
            ).fetchone()
    finally:
        command.upgrade(cfg, "head")
        with _connect() as conn:
            conn.execute("DELETE FROM knowledge_sources WHERE id = %s", (source_id,))
            conn.commit()
    assert gone == (None,)
    assert claim == (None, None, None, None)
