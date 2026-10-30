"""The upgrade-with-data gate (ADR-0024, G13, ticket #218): upgrading a seeded database from
`main`'s head to this branch's head keeps every seeded row with its values.

The gate itself is `test_upgrade_from_main_keeps_every_seeded_row`. The other tests prove it
catches what it must, each with one scratch revision written on top of head in a copy of
`migrations/` under `tmp_path`; nothing is ever written to the real `migrations/`."""

import shutil
import subprocess
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy.engine import make_url

from ai_trainer.adapters.health import to_psycopg_dsn
from tests.migrations import seed as seed_module
from tests.migrations import upgrade_check
from tests.migrations.seed import SEED, SeedError
from tests.migrations.upgrade_check import (
    CONTRACT_EXEMPT_COLUMNS,
    SHARED_DATABASE_URL,
    THROWAWAY_DATABASE,
    UpgradeCheckError,
    base_head,
    run_upgrade_check,
)


def _config() -> Config:
    return Config("alembic.ini")


def _head(config: Config) -> str:
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def _config_with_scratch_revision(tmp_path: Path, upgrade_body: str) -> tuple[Config, str]:
    """A copy of `migrations/` with one revision on top of head; returns its config and head."""
    head = _head(_config())
    scripts = tmp_path / "migrations"
    shutil.copytree("migrations", scripts, ignore=shutil.ignore_patterns("__pycache__"))
    body = textwrap.indent(textwrap.dedent(upgrade_body).strip(), "    ")
    (scripts / "versions" / "0000scratch218_scratch.py").write_text(
        '"""Scratch revision for the upgrade-with-data test (#218)."""\n\n'
        "import sqlalchemy as sa\n"
        "from alembic import op\n\n"
        'revision = "0000scratch218"\n'
        f'down_revision = "{head}"\n'
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        f"def upgrade() -> None:\n{body}\n\n\n"
        "def downgrade() -> None:\n    pass\n"
    )
    config = _config()
    config.set_main_option("script_location", str(scripts))
    return config, head


def test_upgrade_from_main_keeps_every_seeded_row() -> None:
    config = _config()

    problems = run_upgrade_check(config, base=base_head(config), head="head")

    assert problems == []


def test_upgrading_from_an_older_head_seeds_the_tables_it_adds() -> None:
    # `d3a7e5c1b942` was main's head before #254 added `athlete_profiles` and
    # `onboarding_answers`: the seed fits the older schema, and the two new tables are seeded
    # after the upgrade for the coverage check.
    assert run_upgrade_check(_config(), base="d3a7e5c1b942", head="head") == []


def test_contract_exemptions_are_named_columns() -> None:
    # Only columns a contract migration explicitly removes are exempt, each by table and name.
    assert all(table and column for table, column in CONTRACT_EXEMPT_COLUMNS)


def _commit_all(repo: Path, message: str) -> None:
    for args in (["add", "-A"], ["commit", "-q", "-m", message]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_base_head_is_the_head_of_the_revisions_at_the_merge_base(tmp_path: Path) -> None:
    # A scratch repository whose `main` holds today's revisions, and a branch that adds one.
    config, head = _config_with_scratch_revision(tmp_path, "pass")
    scratch = tmp_path / "migrations" / "versions" / "0000scratch218_scratch.py"
    held = scratch.read_text()
    scratch.unlink()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    for key, value in (("user.name", "Seed"), ("user.email", "seed@example.com")):
        subprocess.run(["git", "config", key, value], cwd=tmp_path, check=True)
    _commit_all(tmp_path, "main")
    subprocess.run(["git", "checkout", "-q", "-b", "branch"], cwd=tmp_path, check=True)
    scratch.write_text(held)
    _commit_all(tmp_path, "branch adds a revision")

    assert base_head(config, base_ref="main", repo=tmp_path) == head
    assert _head(config) == "0000scratch218"


def test_seed_columns_missing_from_the_schema_fail_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = tuple({**row, "nmae": "typo"} for row in SEED["goals"])
    monkeypatch.setattr(upgrade_check, "SEED", {**SEED, "goals": rows})
    config = _config()

    problems = run_upgrade_check(config, base=_head(config), head="head")

    assert "goals.nmae: seeded but not in the schema" in problems, problems


def test_base_head_names_a_missing_base_ref() -> None:
    with pytest.raises(UpgradeCheckError, match="no-such-ref"):
        base_head(_config(), base_ref="no-such-ref")


def test_a_revision_dropping_a_seeded_column_fails_naming_it(tmp_path: Path) -> None:
    config, head = _config_with_scratch_revision(tmp_path, 'op.drop_column("goals", "text")')

    problems = run_upgrade_check(config, base=head, head="head")

    assert any("goals.text" in problem and "dropped" in problem for problem in problems), problems


def test_a_revision_dropping_a_primary_key_column_fails_naming_it(tmp_path: Path) -> None:
    config, head = _config_with_scratch_revision(
        tmp_path, 'op.drop_column("weekly_availability", "weekday")'
    )

    problems = run_upgrade_check(config, base=head, head="head")

    assert "weekly_availability.weekday: column dropped" in problems, problems


def test_a_revision_dropping_only_a_primary_key_constraint_loses_nothing(tmp_path: Path) -> None:
    config, head = _config_with_scratch_revision(
        tmp_path, 'op.drop_constraint("weekly_availability_pkey", "weekly_availability")'
    )

    assert run_upgrade_check(config, base=head, head="head") == []


def test_a_revision_dropping_a_seeded_table_fails_naming_it(tmp_path: Path) -> None:
    config, head = _config_with_scratch_revision(tmp_path, 'op.drop_table("weekly_availability")')

    problems = run_upgrade_check(config, base=head, head="head")

    assert any(
        problem.startswith("weekly_availability:") and "dropped" in problem for problem in problems
    ), problems


def test_a_revision_updating_a_user_column_without_where_fails(tmp_path: Path) -> None:
    config, head = _config_with_scratch_revision(
        tmp_path, "op.execute(\"UPDATE training_sessions SET notes = 'overwritten'\")"
    )

    problems = run_upgrade_check(config, base=head, head="head")

    assert any(
        "training_sessions.notes" in problem and "'overwritten'" in problem for problem in problems
    ), problems


@pytest.mark.parametrize(
    ("statement", "column"),
    [
        ("UPDATE session_drafts SET status = 'confirmed'", "session_drafts.status"),
        ("UPDATE chat_messages SET role = 'user'", "chat_messages.role"),
        ("UPDATE users SET email = lower(email)", "users.email"),
        ("UPDATE users SET name = trim(name)", "users.name"),
        ("UPDATE goals SET created_at = date_trunc('minute', created_at)", "goals.created_at"),
        (
            "UPDATE training_sessions SET payload = jsonb_strip_nulls(payload)",
            "training_sessions.payload",
        ),
        ("UPDATE athlete_profiles SET limits = NULL", "athlete_profiles.limits"),
    ],
)
def test_a_backfill_to_one_users_value_still_fails(
    tmp_path: Path, statement: str, column: str
) -> None:
    # Each user holds a different value in every seeded column, so a statement that sets one
    # user's value, or normalizes text, time or JSON, still changes the other user's row.
    config, head = _config_with_scratch_revision(tmp_path, f"op.execute({statement!r})")

    problems = run_upgrade_check(config, base=head, head="head")

    assert any(problem.startswith(f"{column}: row") for problem in problems), problems


def test_every_seeded_column_differs_between_users() -> None:
    for table, rows in SEED.items():
        for column in rows[0]:
            values = {repr(getattr(row[column], "obj", row[column])) for row in rows}
            assert len(values) == len(rows), f"{table}.{column} repeats a value"


def test_a_revision_deleting_seeded_rows_fails_naming_the_rows(tmp_path: Path) -> None:
    config, head = _config_with_scratch_revision(tmp_path, 'op.execute("DELETE FROM goals")')

    problems = run_upgrade_check(config, base=head, head="head")

    assert sum(problem.startswith("goals: row") for problem in problems) == 2, problems


def test_a_new_user_owned_table_without_seed_rows_fails_coverage(tmp_path: Path) -> None:
    config, head = _config_with_scratch_revision(
        tmp_path,
        """\
        op.create_table(
            "scratch_notes",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Uuid(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
        )
        """,
    )

    problems = run_upgrade_check(config, base=head, head="head")

    assert any("scratch_notes" in problem and "seed" in problem for problem in problems), problems


def test_a_table_owned_through_a_composite_foreign_key_needs_seed_rows(tmp_path: Path) -> None:
    # No direct foreign key to `users`: it reaches it through `conversations`.
    config, head = _config_with_scratch_revision(
        tmp_path,
        """\
        op.create_table(
            "scratch_notes",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("conversation_id", sa.Uuid(), nullable=False),
            sa.ForeignKeyConstraint(
                ["conversation_id", "user_id"], ["conversations.id", "conversations.user_id"]
            ),
        )
        """,
    )

    problems = run_upgrade_check(config, base=head, head="head")

    assert any(problem.startswith("scratch_notes:") for problem in problems), problems


def test_a_seed_row_that_does_not_fit_names_its_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, head = _config_with_scratch_revision(
        tmp_path,
        """\
        op.create_table(
            "scratch_notes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        )
        """,
    )
    monkeypatch.setattr(
        seed_module, "SEED", {**seed_module.SEED, "scratch_notes": ({"id": "not-an-int"},)}
    )

    with pytest.raises(SeedError, match="scratch_notes: seed row 0"):
        run_upgrade_check(config, base=head, head="head")


def test_an_additive_revision_passes(tmp_path: Path) -> None:
    # Expand-only changes ship in one release (ADR-0024): a new nullable column loses nothing.
    config, head = _config_with_scratch_revision(
        tmp_path, 'op.add_column("goals", sa.Column("note", sa.String(), nullable=True))'
    )

    assert run_upgrade_check(config, base=head, head="head") == []


def _shared_fingerprint() -> tuple[object, ...]:
    """The shared database's revision and the row count of each of its tables."""
    with psycopg.connect(to_psycopg_dsn(SHARED_DATABASE_URL)) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )
        ]
        version = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        counts = [
            conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()
            for table in tables
        ]
        return (version, *zip(tables, counts, strict=True))


def _database_exists(name: str) -> bool:
    with psycopg.connect(to_psycopg_dsn(SHARED_DATABASE_URL)) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        return row is not None


def test_the_check_refuses_to_migrate_any_database_but_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def _not_repointed(url: str) -> Iterator[None]:
        yield

    monkeypatch.setattr(upgrade_check, "_database_url", _not_repointed)
    before = _shared_fingerprint()

    with pytest.raises(UpgradeCheckError, match="throwaway"):
        run_upgrade_check(_config(), base="d3a7e5c1b942", head="head")

    assert _shared_fingerprint() == before


def test_the_check_leaves_the_shared_database_untouched() -> None:
    config = _config()
    before = _shared_fingerprint()

    run_upgrade_check(config, base=_head(config), head="head")

    assert _shared_fingerprint() == before
    assert not _database_exists(THROWAWAY_DATABASE)
    assert make_url(SHARED_DATABASE_URL).database != THROWAWAY_DATABASE
