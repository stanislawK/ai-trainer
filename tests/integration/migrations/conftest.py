from collections.abc import Iterator

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

# Read once at import, before any fixture can repoint `DATABASE_URL`: the database the app
# and every other integration test share (`make start` serves it too).
_SHARED_DATABASE_URL = str(Settings().database_url)


def _run_on_shared_database(statement: sql.Composed) -> None:
    # CREATE/DROP DATABASE can't run inside a transaction block, hence autocommit.
    with psycopg.connect(to_psycopg_dsn(_SHARED_DATABASE_URL), autocommit=True) as conn:
        conn.execute(statement)


def _drop_database(name: str) -> None:
    _run_on_shared_database(
        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
    )


@pytest.fixture(scope="session")
def shared_database_url() -> str:
    return _SHARED_DATABASE_URL


@pytest.fixture(scope="module", autouse=True)
def _scratch_database() -> Iterator[None]:
    """Runs each migration test module against its own throwaway `<db>_migrations` database
    at head (ADR-0013 amendment, 2026-10-09): their downgrades drop tables, and on the shared
    database that wiped the developer's users and sessions. `Settings()` reads the environment
    ahead of `.env`, so both the tests' own helpers and `migrations/env.py` follow the
    repointed `DATABASE_URL`. Dropping first clears a database left by a crashed earlier run."""
    shared = make_url(_SHARED_DATABASE_URL)
    scratch_name = f"{shared.database}_migrations"
    scratch_url = shared.set(database=scratch_name).render_as_string(hide_password=False)
    _drop_database(scratch_name)
    _run_on_shared_database(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch_name)))
    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DATABASE_URL", scratch_url)
            command.upgrade(Config("alembic.ini"), "head")
            yield
    finally:
        _drop_database(scratch_name)
