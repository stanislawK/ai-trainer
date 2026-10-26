"""Migration `e8b1f4c7a2d9` (ADR-0030, ADR-0024, ticket #273): the login role Alloy's Postgres
exporter connects as, created expand-only and holding `pg_monitor` alone. It can read the
statistics views and no app table. Its password is set outside the migration, from alloy.env
(`deploy/monitor-password.sh`), so none is in the repo."""

from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_PREVIOUS_REVISION = "7d2c9e4a1f63"
ROLE = "ai_trainer_monitor"


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _role() -> tuple[bool, bool, bool, bool, bool, list[str]] | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT r.rolcanlogin, r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolbypassrls,"
            " array(SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid"
            "       WHERE m.member = r.oid ORDER BY 1)"
            " FROM pg_roles r WHERE r.rolname = %s",
            (ROLE,),
        )
        row = cur.fetchone()
        return None if row is None else (row[0], row[1], row[2], row[3], row[4], list(row[5]))


def test_the_role_logs_in_with_pg_monitor_and_nothing_else() -> None:
    assert _role() == (True, False, False, False, False, ["pg_monitor"])


def test_the_role_cannot_read_an_app_table() -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(ROLE)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT 1 FROM users LIMIT 1")


def test_the_role_has_no_privilege_on_any_relation_in_the_database() -> None:
    """Every table, view, materialized view and sequence, today's and any a later migration
    adds, down to a single column, unless one grants it explicitly."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT c.oid::regclass::text FROM pg_class c"
            " JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')"
            " AND n.nspname NOT LIKE 'pg_toast%%'"
            " AND ("
            "   (c.relkind IN ('r', 'p', 'v', 'm', 'f')"
            "    AND (has_table_privilege(%(role)s, c.oid, 'SELECT, INSERT, UPDATE, DELETE,"
            "           TRUNCATE')"
            "         OR has_any_column_privilege(%(role)s, c.oid, 'SELECT, INSERT, UPDATE')))"
            "   OR (c.relkind = 'S'"
            "       AND has_sequence_privilege(%(role)s, c.oid, 'SELECT, USAGE, UPDATE'))"
            "   OR has_schema_privilege(%(role)s, n.oid, 'CREATE'))",
            {"role": ROLE},
        )
        assert cur.fetchall() == []
        cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] > 0


def test_the_role_reads_the_statistics_the_exporter_scrapes() -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(ROLE)))
        cur.execute("SELECT numbackends FROM pg_stat_database WHERE datname = current_database()")
        assert cur.fetchone() is not None
        # pg_read_all_stats, through pg_monitor: other sessions' rows are not redacted.
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE state IS NOT NULL")
        row = cur.fetchone()
        assert row is not None
        assert row[0] >= 1


def test_downgrade_drops_the_role_and_upgrade_recreates_it() -> None:
    cfg = Config("alembic.ini")
    try:
        command.downgrade(cfg, _PREVIOUS_REVISION)
        assert _role() is None
        command.upgrade(cfg, "head")
        assert _role() == (True, False, False, False, False, ["pg_monitor"])
    finally:
        command.upgrade(cfg, "head")


def test_an_existing_role_is_kept_on_upgrade_with_no_more_rights() -> None:
    """Roles belong to the cluster, not to one database: every other database migrated on it
    (the shared one, the throwaway ones) already made it, and a rerun must not fail. One made
    by hand with more rights is cut back to the monitor's."""
    cfg = Config("alembic.ini")
    try:
        command.downgrade(cfg, _PREVIOUS_REVISION)
        with _connect() as conn:
            conn.execute(
                sql.SQL("CREATE ROLE {} NOLOGIN SUPERUSER CREATEDB CREATEROLE BYPASSRLS").format(
                    sql.Identifier(ROLE)
                )
            )
        command.upgrade(cfg, "head")
        assert _role() == (True, False, False, False, False, ["pg_monitor"])
    finally:
        command.upgrade(cfg, "head")
