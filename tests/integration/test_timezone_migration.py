import psycopg
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_PREVIOUS_REVISION = "3b8e5d4f7a91"


def _timezone_column() -> list[tuple[str, str]]:
    dsn = to_psycopg_dsn(str(Settings().database_url))
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_default, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'timezone'"
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def test_upgrade_adds_a_non_null_timezone_defaulting_to_utc() -> None:
    cfg = Config("alembic.ini")
    command.downgrade(cfg, _PREVIOUS_REVISION)

    try:
        command.upgrade(cfg, "head")
        assert _timezone_column() == [("'UTC'::character varying", "NO")]
    finally:
        command.upgrade(cfg, "head")


def test_downgrade_removes_the_timezone_column() -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    try:
        command.downgrade(cfg, _PREVIOUS_REVISION)
        assert _timezone_column() == []
    finally:
        command.upgrade(cfg, "head")
