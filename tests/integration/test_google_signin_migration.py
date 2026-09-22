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
            "WHERE table_schema = 'public' AND table_name = 'sessions'"
        )
        return {row[0] for row in cur.fetchall()}


def _user_columns() -> set[str]:
    dsn = to_psycopg_dsn(str(Settings().database_url))
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'users'"
        )
        return {row[0] for row in cur.fetchall()}


def test_upgrade_head_adds_sessions_table_and_user_columns() -> None:
    cfg = _alembic_config()
    command.downgrade(cfg, "fde2eaf4fd78")

    try:
        command.upgrade(cfg, "head")
        assert _tables_present() == {"sessions"}
        assert {"sub", "email", "name", "locale", "status"} <= _user_columns()
    finally:
        command.upgrade(cfg, "head")


def test_downgrade_reverses_sessions_and_user_columns_cleanly() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    try:
        command.downgrade(cfg, "fde2eaf4fd78")
        assert _tables_present() == set()
        assert _user_columns() == {"id", "created_at"}
    finally:
        command.upgrade(cfg, "head")


def test_upgrade_head_run_twice_is_a_noop() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    try:
        command.upgrade(cfg, "head")  # must not raise
        assert _tables_present() == {"sessions"}
    finally:
        command.upgrade(cfg, "head")
