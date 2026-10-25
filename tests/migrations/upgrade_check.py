"""The upgrade-with-data check (ADR-0024, ticket #218).

1. Migrate a throwaway database to `main`'s head: the head of the revision files present at
   `git merge-base origin/main HEAD`, the same base `make squawk` diffs against.
2. Seed it with `tests/migrations/seed.py` and read back every row of every user-owned table.
3. Upgrade to the branch head and compare: a dropped table or column, a lost row or a changed
   value is a problem, named by table, column and primary key. Rows are matched on the primary
   key they had before the upgrade, so a migration that drops a key column is named for it.
4. Coverage: every user-owned table at the branch head has seed rows for both users. Tables new
   on the branch are seeded after the upgrade, so their rows are proven to fit before the next
   branch relies on them.

A user-owned table is `users` or any table with a foreign key to a user-owned table, so a child
that reaches `users` only through its parent counts too. The throwaway database sits next to the
shared one, is named per process so two runs can't drop each other's, and is dropped afterwards;
the shared database is only used to create and drop it.
"""

import os
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from alembic import command, util
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy.engine import make_url

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings
from tests.migrations.seed import SEED, USERS, columns, seed

# Read once at import, before the check repoints `DATABASE_URL`: the database the app and every
# other integration test share (`make start` serves it too).
SHARED_DATABASE_URL = str(Settings().database_url)
THROWAWAY_DATABASE = f"{make_url(SHARED_DATABASE_URL).database}_upgrade_{os.getpid()}"

# Columns a contract migration removes on purpose (ADR-0024), as (table, column), each with the
# contract ticket in a comment. Nothing else is exempt.
CONTRACT_EXEMPT_COLUMNS: frozenset[tuple[str, str]] = frozenset()

_VERSIONS = "migrations/versions"


@dataclass(frozen=True)
class TableRows:
    """One table's columns and rows, each row keyed by the values of `key`'s columns."""

    key: tuple[str, ...]
    columns: tuple[str, ...]
    rows: dict[tuple[object, ...], dict[str, object]]


Snapshot = dict[str, TableRows]


class UpgradeCheckError(Exception):
    """The check could not run: no base ref, no single head at the base, or it would have
    migrated a database other than its own throwaway one."""


def base_head(config: Config, base_ref: str = "origin/main", repo: Path = Path()) -> str:
    """The head of the revisions whose files exist at the merge base of `base_ref` and HEAD."""
    try:
        merge_base = _git(repo, "merge-base", base_ref, "HEAD").strip()
        names = set(_git(repo, "ls-tree", "--name-only", f"{merge_base}:{_VERSIONS}").splitlines())
    except subprocess.CalledProcessError as exc:
        raise UpgradeCheckError(
            f"cannot find the merge base with {base_ref!r} ({exc.stderr.strip()}); "
            "run `git fetch origin main` first"
        ) from exc
    scripts = [
        script
        for script in ScriptDirectory.from_config(config).walk_revisions()
        if Path(script.path).name in names
    ]
    parents = {down for script in scripts for down in util.to_tuple(script.down_revision, ())}
    heads = sorted(script.revision for script in scripts if script.revision not in parents)
    if len(heads) != 1:
        raise UpgradeCheckError(f"expected one head at {merge_base[:12]}, found {heads}")
    return heads[0]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def run_upgrade_check(config: Config, base: str, head: str) -> list[str]:
    """Every problem upgrading a seeded database from `base` to `head` finds; empty passes."""
    with _throwaway_database() as url, _database_url(url):
        if make_url(str(Settings().database_url)).database != THROWAWAY_DATABASE:
            raise UpgradeCheckError("DATABASE_URL does not point at the throwaway database")
        command.upgrade(config, base)
        with psycopg.connect(to_psycopg_dsn(url)) as conn:
            base_tables = user_owned_tables(conn)
            seed(conn, base_tables)
            conn.commit()
            before = snapshot(conn, base_tables)
        command.upgrade(config, head)
        with psycopg.connect(to_psycopg_dsn(url)) as conn:
            head_tables = user_owned_tables(conn)
            after = snapshot(
                conn,
                base_tables & head_tables,
                {table: before[table].key for table in base_tables},
            )
            problems = compare(before, after, CONTRACT_EXEMPT_COLUMNS)
            seed(conn, head_tables - base_tables)
            conn.commit()
            problems += coverage_problems(conn, head_tables)
    return problems


@contextmanager
def _throwaway_database() -> Iterator[str]:
    # Dropping first clears a database left by a crashed earlier run.
    _drop(THROWAWAY_DATABASE)
    _on_shared(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(THROWAWAY_DATABASE)))
    try:
        yield (
            make_url(SHARED_DATABASE_URL)
            .set(database=THROWAWAY_DATABASE)
            .render_as_string(hide_password=False)
        )
    finally:
        _drop(THROWAWAY_DATABASE)


def _drop(name: str) -> None:
    _on_shared(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _on_shared(statement: sql.Composed) -> None:
    # CREATE/DROP DATABASE can't run inside a transaction block, hence autocommit.
    with psycopg.connect(to_psycopg_dsn(SHARED_DATABASE_URL), autocommit=True) as conn:
        conn.execute(statement)


@contextmanager
def _database_url(url: str) -> Iterator[None]:
    """`migrations/env.py` reads the URL through `Settings()`, which reads the environment."""
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if previous is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = previous


def user_owned_tables(conn: psycopg.Connection[Any]) -> set[str]:
    """`users` and every table that reaches it through foreign keys."""
    edges = conn.execute(
        "SELECT conrelid::regclass::text, confrelid::regclass::text FROM pg_constraint "
        "WHERE contype = 'f'"
    ).fetchall()
    owned = {"users"}
    while grown := {child for child, parent in edges if parent in owned} - owned:
        owned |= grown
    return owned


def _primary_key(conn: psycopg.Connection[Any], table: str) -> tuple[str, ...]:
    cursor = conn.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = to_regclass(%s) AND i.indisprimary ORDER BY a.attname",
        (f"public.{table}",),
    )
    return tuple(row[0] for row in cursor)


def snapshot(
    conn: psycopg.Connection[Any],
    tables: set[str],
    keys: Mapping[str, Sequence[str]] | None = None,
) -> Snapshot:
    """Every row of `tables` with every column's value, keyed by the table's primary key, or by
    the columns `keys` names for it (the key it had before an upgrade) that still exist."""
    result: Snapshot = {}
    for table in sorted(tables):
        cursor = conn.cursor(row_factory=psycopg.rows.dict_row)
        cursor.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table)))
        columns = tuple(column.name for column in cursor.description or ())
        wanted = keys[table] if keys and table in keys else _primary_key(conn, table)
        key = tuple(column for column in wanted if column in columns)
        rows = {tuple(row[c] for c in key): dict(row) for row in cursor}
        result[table] = TableRows(key=key, columns=columns, rows=rows)
    return result


def compare(before: Snapshot, after: Snapshot, exempt: frozenset[tuple[str, str]]) -> list[str]:
    """What the upgrade lost or changed in the rows seen before it."""
    problems: list[str] = []
    for table, old in before.items():
        new = after.get(table)
        if new is None:
            problems.append(f"{table}: table dropped with {len(old.rows)} seeded row(s)")
            continue
        problems += [
            f"{table}.{column}: column dropped"
            for column in old.columns
            if column not in new.columns and (table, column) not in exempt
        ]
        if not new.key:
            problems.append(f"{table}: no key column left to match its seeded rows on")
            continue
        # Match on the key columns that survived (`new.key`), even when the key itself changed.
        positions = [old.key.index(column) for column in new.key]
        for key, row in old.rows.items():
            matched = tuple(key[i] for i in positions)
            now = new.rows.get(matched)
            if now is None:
                problems.append(f"{table}: row {_key(key)} lost")
                continue
            problems += [
                f"{table}.{column}: row {_key(key)} changed {value!r} -> {now[column]!r}"
                for column, value in row.items()
                if column in now and (table, column) not in exempt and now[column] != value
            ]
    return problems


def _key(key: Sequence[object]) -> str:
    return "(" + ", ".join(str(part) for part in key) + ")"


def coverage_problems(conn: psycopg.Connection[Any], tables: set[str]) -> list[str]:
    """User-owned tables without seed rows for both users, and seed columns the branch head
    doesn't have (a typo there would otherwise leave a column unseeded)."""
    problems = [
        f"{table}: user-owned table has no rows in tests/migrations/seed.py"
        for table in sorted(tables - SEED.keys())
    ]
    for table in sorted(tables & SEED.keys()):
        present = columns(conn, table)
        problems += [
            f"{table}.{column}: seeded but not in the schema"
            for column in sorted({column for row in SEED[table] for column in row} - present)
            if (table, column) not in CONTRACT_EXEMPT_COLUMNS
        ]
        users = _users_in(conn, table)
        if users is None:
            if len(SEED[table]) < len(USERS):
                problems.append(f"{table}: needs a seed row per user ({len(USERS)})")
            continue
        missing = [user for user in USERS if user not in users]
        if missing:
            problems.append(f"{table}: seed rows miss user(s) {missing}")
    return problems


def _users_in(conn: psycopg.Connection[Any], table: str) -> set[object] | None:
    """User ids a table's rows hold in `user_id` or a foreign key to `users`; None when it has
    no such column (a child owned only through its parent)."""
    if table == "users":
        return {row[0] for row in conn.execute("SELECT id FROM users")}
    columns = {
        row[0]
        for row in conn.execute(
            "SELECT a.attname FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
            "WHERE c.contype = 'f' AND c.conrelid = to_regclass(%s) "
            "AND c.confrelid = to_regclass('public.users') "
            "UNION SELECT attname FROM pg_attribute "
            "WHERE attrelid = to_regclass(%s) AND attname = 'user_id' AND NOT attisdropped",
            (f"public.{table}", f"public.{table}"),
        )
    }
    if not columns:
        return None
    users: set[object] = set()
    for column in columns:
        cursor = conn.execute(
            sql.SQL("SELECT DISTINCT {} FROM {}").format(
                sql.Identifier(column), sql.Identifier(table)
            )
        )
        users |= {row[0] for row in cursor}
    return users
