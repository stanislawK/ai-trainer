"""Migrations `b7d41e9c2f63` and `c3a85f0d6e17` (ADR-0036's #454 amendment, ADR-0024, #456):
a source may wait for its licence while it is `detecting` or `proposed`, and only then. #422's
and #453's rows keep their licence. Upgrading with data in every table is `tests/migrations/`'
job (#218)."""

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

_PREVIOUS_REVISION = "9e4b2c7a1d58"
_AT = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _insert_source(conn: psycopg.Connection[Any], **columns: object) -> UUID:
    source_id = uuid4()
    values: dict[str, object] = {
        "id": source_id,
        "title": "Strength programming notes",
        "source_type": "book",
        "evidence_tier": "coaching_book",
        "language": "en",
        "sports": ["gym"],
        "content_hash": uuid4().hex * 2,
        "uploaded_at": _AT,
        **columns,
    }
    names = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    conn.execute(
        f"INSERT INTO knowledge_sources ({names}) VALUES ({placeholders})",
        list(values.values()),
    )
    return source_id


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[Any]]:
    """A connection whose writes are rolled back, so a refused insert leaves nothing behind."""
    with _connect() as connection:
        yield connection
        connection.rollback()


def test_422_rows_keep_their_licence_through_the_upgrade() -> None:
    cfg = Config("alembic.ini")
    command.downgrade(cfg, _PREVIOUS_REVISION)
    try:
        with _connect() as conn:
            # #422's insert names no licence column; #453's names both.
            old = _insert_source(conn, license_note="Owner's copy", status="ingested")
            staged = _insert_source(
                conn, status="proposed", license_id="cc-by-sa", license_source="pmc"
            )
            conn.commit()

        command.upgrade(cfg, "head")

        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, status, license_id, license_source, license_note,"
                " license_matched_line, license_locator, license_detection_reason"
                " FROM knowledge_sources WHERE id = ANY(%s) ORDER BY status",
                ([old, staged],),
            ).fetchall()
            conn.execute("DELETE FROM knowledge_sources WHERE id = ANY(%s)", ([old, staged],))
            conn.commit()
        assert rows == [
            (old, "ingested", "other", "admin", "Owner's copy", None, None, None),
            (staged, "proposed", "cc-by-sa", "pmc", None, None, None, None),
        ]
    finally:
        command.upgrade(cfg, "head")


@pytest.mark.parametrize("status", ["detecting", "proposed"])
def test_a_detecting_or_proposed_source_may_wait_without_a_licence(
    conn: psycopg.Connection[Any], status: str
) -> None:
    source_id = _insert_source(
        conn,
        status=status,
        license_id=None,
        license_source=None,
        license_detection_reason="No licence found in the front matter.",
    )

    row = conn.execute(
        "SELECT status, license_id, license_source FROM knowledge_sources WHERE id = %s",
        (source_id,),
    ).fetchone()
    assert row == (status, None, None)


@pytest.mark.parametrize("status", ["uploaded", "ingesting", "ingested", "failed", "rejected"])
def test_a_licence_is_required_unless_detecting_or_proposed(
    conn: psycopg.Connection[Any], status: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="knowledge_sources_licence_set"):
        _insert_source(conn, status=status, license_id=None, license_source=None)


@pytest.mark.parametrize(
    ("license_id", "license_source"), [("cc-by", None), (None, "file")], ids=["no-source", "no-id"]
)
def test_licence_id_and_source_are_set_together(
    conn: psycopg.Connection[Any], license_id: str | None, license_source: str | None
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="knowledge_sources_licence_pair"):
        _insert_source(
            conn, status="proposed", license_id=license_id, license_source=license_source
        )


def test_license_source_accepts_file_with_what_was_matched(
    conn: psycopg.Connection[Any],
) -> None:
    source_id = _insert_source(
        conn,
        status="proposed",
        license_id="cc-by-nc-nd",
        license_source="file",
        attribution="K. Marsh, Hypertrophy handbook, ch. 2, CC BY-NC-ND 4.0",
        license_matched_line="This chapter is licensed under CC BY-NC-ND 4.0.",
        license_locator="page 2",
    )

    row = conn.execute(
        "SELECT license_source, license_matched_line, license_locator"
        " FROM knowledge_sources WHERE id = %s",
        (source_id,),
    ).fetchone()
    assert row == ("file", "This chapter is licensed under CC BY-NC-ND 4.0.", "page 2")


def test_an_unknown_status_or_licence_source_is_still_refused(
    conn: psycopg.Connection[Any],
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="knowledge_sources_status"):
        _insert_source(conn, status="parsing", license_id="cc0")
    conn.rollback()
    with pytest.raises(psycopg.errors.CheckViolation, match="knowledge_sources_license_source"):
        _insert_source(conn, license_id="cc0", license_source="europepmc")
