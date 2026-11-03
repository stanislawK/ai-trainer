"""`knowledge_sources` upgrades and downgrades cleanly, expand only (ADR-0024, #422)."""

import psycopg
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_PREVIOUS_REVISION = "dcdd9ed83df3"


def _alembic_config() -> Config:
    return Config("alembic.ini")


def _query(sql: str) -> set[str]:
    dsn = to_psycopg_dsn(str(Settings().database_url))
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return {row[0] for row in cur.fetchall()}


def _has_table() -> set[str]:
    return _query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'knowledge_sources'"
    )


def _constraints() -> set[str]:
    return _query(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'knowledge_sources'::regclass"
    )


def test_upgrade_adds_knowledge_sources_with_its_checks() -> None:
    cfg = _alembic_config()
    command.downgrade(cfg, _PREVIOUS_REVISION)

    try:
        command.upgrade(cfg, "head")
        assert _has_table() == {"knowledge_sources"}
        assert {
            "knowledge_sources_source_type",
            "knowledge_sources_evidence_tier",
            "knowledge_sources_language",
            "knowledge_sources_status",
            "knowledge_sources_file_type",
            "knowledge_sources_sports_not_empty",
            "knowledge_sources_license_note",
            "knowledge_sources_content_hash_key",
            "knowledge_sources_storage_key_key",
        } <= _constraints()
    finally:
        command.upgrade(cfg, "head")


def test_downgrade_removes_knowledge_sources_cleanly() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    try:
        command.downgrade(cfg, _PREVIOUS_REVISION)
        assert _has_table() == set()
    finally:
        command.upgrade(cfg, "head")
