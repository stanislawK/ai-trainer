"""Migrations `5c1e9b7d3a20` and `9e4b2c7a1d58` (ADR-0036, ADR-0024, ticket #453): the knowledge
schema for staged sources, expand only. #422's sources become `other` with their note; the new
CHECKs and unique identifiers hold; `admin_tokens` goes with its user. Upgrading with data in
every table is `tests/migrations/`' job (#218)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_REVISION = "9e4b2c7a1d58"
_PREVIOUS_REVISION = "2e3b50c913e0"
_AT = datetime(2026, 11, 3, 9, 15, 30, 123456, tzinfo=UTC)

_INSERT_422_SOURCE = (
    "INSERT INTO knowledge_sources (id, title, source_type, evidence_tier, language, sports,"
    " license_note, content_hash, status, uploaded_at)"
    " VALUES (%s, %s, 'book', 'coaching_book', 'pl', ARRAY['gym'], %s, %s, %s, %s)"
)


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _insert_source(conn: psycopg.Connection[Any], **columns: object) -> UUID:
    source_id = uuid4()
    values: dict[str, object] = {
        "id": source_id,
        "title": "Deload timing",
        "source_type": "research_paper",
        "evidence_tier": "peer_reviewed",
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


def test_upgrade_keeps_422_sources_as_other_with_their_note() -> None:
    cfg = Config("alembic.ini")
    command.downgrade(cfg, _PREVIOUS_REVISION)
    try:
        ids = (uuid4(), uuid4())
        with _connect() as conn:
            for n, (source_id, note, status) in enumerate(
                zip(ids, ("CC BY 4.0", "  Owner's copy "), ("ingested", "failed"), strict=True)
            ):
                conn.execute(
                    _INSERT_422_SOURCE,
                    (source_id, f"Source {n}", note, str(n) * 64, status, _AT),
                )
            conn.commit()

        command.upgrade(cfg, "head")

        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, status, license_id, license_source, license_note"
                " FROM knowledge_sources WHERE id = ANY(%s) ORDER BY title",
                (list(ids),),
            ).fetchall()
            conn.execute("DELETE FROM knowledge_sources WHERE id = ANY(%s)", (list(ids),))
            conn.commit()
        assert rows == [
            (ids[0], "ingested", "other", "admin", "CC BY 4.0"),
            (ids[1], "failed", "other", "admin", "  Owner's copy "),
        ]
    finally:
        command.upgrade(cfg, "head")


def test_other_without_a_note_is_refused(conn: psycopg.Connection[Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="other_license_note"):
        _insert_source(conn, license_id="other", license_note=None)


def test_a_blank_note_is_still_refused(conn: psycopg.Connection[Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="knowledge_sources_license_note"):
        _insert_source(conn, license_id="cc-by", license_note="   ")


def test_an_unknown_license_id_is_refused(conn: psycopg.Connection[Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="knowledge_sources_license_id"):
        _insert_source(conn, license_id="cc-by-4.0", license_note="CC BY 4.0")


def test_an_unknown_license_source_is_refused(conn: psycopg.Connection[Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match="knowledge_sources_license_source"):
        _insert_source(conn, license_id="cc-by", license_source="europepmc")


def test_a_cc_source_needs_no_note_and_takes_every_new_field(
    conn: psycopg.Connection[Any],
) -> None:
    source_id = _insert_source(
        conn,
        status="proposed",
        file_type="jats",
        license_id="cc-by-nc-nd",
        license_source="pmc",
        license_url="https://creativecommons.org/licenses/by-nc-nd/4.0/",
        copyright_statement="© 2025 The Authors",
        attribution="M. Kowalska et al., Deload timing. Source: NLM PMC",
        pmcid="PMC13692514",
        doi="10.1000/deload.2025",
        pmid="41234567",
        isbn="9788312345678",
    )

    row = conn.execute(
        "SELECT status, file_type, license_id, license_note FROM knowledge_sources WHERE id = %s",
        (source_id,),
    ).fetchone()
    assert row == ("proposed", "jats", "cc-by-nc-nd", None)


def test_a_rejected_source_is_a_known_status(conn: psycopg.Connection[Any]) -> None:
    _insert_source(conn, status="rejected", license_id="cc0")


@pytest.mark.parametrize(
    ("column", "value"),
    [("pmcid", "PMC123"), ("doi", "10.1000/same"), ("pmid", "123"), ("isbn", "9780000000002")],
)
def test_a_second_source_with_the_same_identifier_is_refused(
    conn: psycopg.Connection[Any], column: str, value: str
) -> None:
    _insert_source(conn, license_id="cc-by", **{column: value})

    with pytest.raises(psycopg.errors.UniqueViolation, match=f"knowledge_sources_{column}_key"):
        _insert_source(conn, license_id="cc-by", **{column: value})


def test_sources_without_identifiers_never_collide(conn: psycopg.Connection[Any]) -> None:
    _insert_source(conn, license_id="cc-by")
    _insert_source(conn, license_id="cc-by")


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("pmcid", "13692514"),
        ("pmcid", "pmc13692514"),
        ("pmid", "PMID41"),
        ("doi", "10.1/ABC"),
        ("doi", "https://doi.org/10.1/abc"),
    ],
)
def test_a_malformed_identifier_is_refused(
    conn: psycopg.Connection[Any], column: str, value: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation, match=f"knowledge_sources_{column}_format"):
        _insert_source(conn, license_id="cc-by", **{column: value})


def _insert_token(conn: psycopg.Connection[Any], user_id: UUID, *, expires_in: timedelta) -> UUID:
    token_id = uuid4()
    conn.execute(
        "INSERT INTO admin_tokens (id, user_id, token_hash, display_prefix, created_at,"
        " expires_at) VALUES (%s, %s, %s, %s, %s, %s)",
        (token_id, user_id, uuid4().hex * 2, "akt_3f9c", _AT, _AT + expires_in),
    )
    return token_id


def _insert_user(conn: psycopg.Connection[Any]) -> UUID:
    user_id = uuid4()
    conn.execute(
        "INSERT INTO users (id, sub, email, name, locale, status, created_at)"
        " VALUES (%s, %s, %s, 'Admin', 'en', 'active', %s)",
        (user_id, f"sub-{user_id}", f"{user_id}@example.com", _AT),
    )
    return user_id


def test_admin_tokens_cascade_with_their_user(conn: psycopg.Connection[Any]) -> None:
    user_id = _insert_user(conn)
    token_id = _insert_token(conn, user_id, expires_in=timedelta(days=30))

    conn.execute("DELETE FROM users WHERE id = %s", (user_id,))

    assert conn.execute("SELECT 1 FROM admin_tokens WHERE id = %s", (token_id,)).fetchone() is None


@pytest.mark.parametrize(
    "expires_in", [timedelta(days=30, seconds=1), timedelta(0)], ids=["past-30-days", "at-once"]
)
def test_a_token_expires_within_30_days_of_its_creation(
    conn: psycopg.Connection[Any], expires_in: timedelta
) -> None:
    user_id = _insert_user(conn)

    with pytest.raises(psycopg.errors.CheckViolation, match="admin_tokens_expires_at"):
        _insert_token(conn, user_id, expires_in=expires_in)


def test_a_token_hash_is_sha256_hex(conn: psycopg.Connection[Any]) -> None:
    user_id = _insert_user(conn)

    with pytest.raises(psycopg.errors.CheckViolation, match="admin_tokens_token_hash"):
        conn.execute(
            "INSERT INTO admin_tokens (id, user_id, token_hash, display_prefix, created_at,"
            " expires_at) VALUES (%s, %s, 'not-a-hash', 'akt_', %s, %s)",
            (uuid4(), user_id, _AT, _AT + timedelta(days=1)),
        )


def test_admin_tokens_are_indexed_by_user() -> None:
    with _connect() as conn:
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'admin_tokens'"
            )
        }
    assert {"ix_admin_tokens_user_id", "admin_tokens_token_hash_key"} <= indexes


def test_downgrade_reverses_the_revision_cleanly() -> None:
    cfg = Config("alembic.ini")
    try:
        command.downgrade(cfg, _REVISION)
        command.downgrade(cfg, _PREVIOUS_REVISION)
        with _connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            }
            columns = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'knowledge_sources'"
                )
            }
        assert "admin_tokens" not in tables
        assert not columns & {"license_id", "license_source", "pmcid", "doi", "pmid", "isbn"}
        command.upgrade(cfg, _REVISION)
    finally:
        command.upgrade(cfg, "head")
