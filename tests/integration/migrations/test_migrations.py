from uuid import uuid4

import psycopg
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings


def _alembic_config() -> Config:
    return Config("alembic.ini")


def _vector_extension_installed() -> bool:
    dsn = to_psycopg_dsn(str(Settings().database_url))
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        return cur.fetchone() is not None


def test_upgrade_head_creates_the_vector_extension() -> None:
    cfg = _alembic_config()
    command.downgrade(cfg, "base")

    try:
        command.upgrade(cfg, "head")
        assert _vector_extension_installed() is True
    finally:
        command.upgrade(cfg, "head")  # leave the database at head even if the assert fails


def test_downgrade_base_reverses_the_vector_extension_cleanly() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    try:
        command.downgrade(cfg, "base")
        assert _vector_extension_installed() is False
    finally:
        command.upgrade(cfg, "head")  # leave the database at head even if the assert fails


def test_round_trip_leaves_the_shared_database_untouched(shared_database_url: str) -> None:
    # A downgrade to base on the shared database dropped every table, signing the developer
    # out of `make start` on every test run (ADR-0013 amendment, 2026-10-09).
    shared_dsn = to_psycopg_dsn(shared_database_url)
    probe_id = uuid4()
    with psycopg.connect(shared_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, sub, email) VALUES (%s, %s, %s)",
            (probe_id, f"migration-probe-{probe_id}", "migration-probe@example.com"),
        )

    try:
        cfg = _alembic_config()
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        with psycopg.connect(shared_dsn) as conn:
            row = conn.execute("SELECT 1 FROM users WHERE id = %s", (probe_id,)).fetchone()
        assert row is not None
    finally:
        command.upgrade(cfg, "head")  # leave the database at head even if the assert fails
        with psycopg.connect(shared_dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM users WHERE id = %s", (probe_id,))


def test_upgrade_head_run_twice_is_a_noop() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    try:
        command.upgrade(cfg, "head")  # must not raise
        assert _vector_extension_installed() is True
    finally:
        command.upgrade(cfg, "head")  # leave the database at head even if the assert fails
