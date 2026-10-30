"""`make worktree`'s Postgres side (ADR-0035, ticket #351): it creates, finds and drops a
worktree's database next to the shared one, and names an unreachable server instead of
creating anything. Its module holds the server-wide test lock, as every module that creates a
database does."""

import os
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.engine import make_url

from scripts import worktree as wt
from tests.server_lock import SERVER_WIDE_LOCK_KEY, server_wide_lock_dsn

SHARED_URL = wt.shared_database_url(Path(__file__).resolve().parents[2])
NAME = f"{make_url(SHARED_URL).database}_wt_test_{os.getpid()}"


@pytest.fixture
def databases() -> wt.PostgresDatabases:
    found = wt.PostgresDatabases(SHARED_URL)
    if found.exists(NAME):
        found.drop(NAME)
    return found


def test_create_exists_and_drop_round_trip(databases: wt.PostgresDatabases) -> None:
    assert not databases.exists(NAME)
    databases.create(NAME)
    try:
        assert databases.exists(NAME)
    finally:
        databases.drop(NAME)
    assert not databases.exists(NAME)


def test_creating_an_existing_database_is_a_reason_to_stop(
    databases: wt.PostgresDatabases,
) -> None:
    databases.create(NAME)
    try:
        with pytest.raises(wt.WorktreeError, match="Postgres refused"):
            databases.create(NAME)
    finally:
        databases.drop(NAME)


def test_unreachable_server_is_named() -> None:
    down = make_url(SHARED_URL).set(port=1).render_as_string(hide_password=False)
    with pytest.raises(wt.DatabaseUnavailableError, match="Postgres"):
        wt.PostgresDatabases(down).exists(NAME)


def test_module_holds_the_server_wide_lock_other_checkouts_wait_on() -> None:
    """Another checkout's run, on another database of the same server, can't take it now."""
    with psycopg.connect(server_wide_lock_dsn(), autocommit=True) as other:
        row = other.execute("SELECT pg_try_advisory_lock(%s)", [SERVER_WIDE_LOCK_KEY]).fetchone()
    assert row == (False,)
