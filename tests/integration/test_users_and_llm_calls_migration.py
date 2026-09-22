import psycopg
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings


def _alembic_config() -> Config:
    return Config("alembic.ini")


def _tables_present() -> set[str]:
    dsn = to_psycopg_dsn(str(Settings().database_url))
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN ('users', 'llm_calls')"
        )
        return {row[0] for row in cur.fetchall()}


def test_upgrade_head_creates_users_and_llm_calls_tables() -> None:
    cfg = _alembic_config()
    command.downgrade(cfg, "49b795678986")

    try:
        command.upgrade(cfg, "head")
        assert _tables_present() == {"users", "llm_calls"}
    finally:
        command.upgrade(cfg, "head")


def test_downgrade_reverses_users_and_llm_calls_tables_cleanly() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    try:
        command.downgrade(cfg, "49b795678986")
        assert _tables_present() == set()
    finally:
        command.upgrade(cfg, "head")


def test_upgrade_head_run_twice_is_a_noop() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    try:
        command.upgrade(cfg, "head")  # must not raise
        assert _tables_present() == {"users", "llm_calls"}
    finally:
        command.upgrade(cfg, "head")
