import psycopg
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_PREVIOUS_REVISION = "5d2f8a1c9e47"


def _has_chat_messages() -> bool:
    dsn = to_psycopg_dsn(str(Settings().database_url))
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.chat_messages') IS NOT NULL")
        row = cur.fetchone()
        return bool(row and row[0])


def test_upgrade_creates_chat_messages_and_downgrade_removes_it() -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    try:
        assert _has_chat_messages()
        command.downgrade(cfg, _PREVIOUS_REVISION)
        assert not _has_chat_messages()
    finally:
        command.upgrade(cfg, "head")
