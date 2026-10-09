import psycopg
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_PREVIOUS_REVISION = "f0ddfe1662db"
_GOALS_REVISION = "1959a40c6d92"


def _alembic_config() -> Config:
    return Config("alembic.ini")


def _query(sql: str) -> set[str]:
    dsn = to_psycopg_dsn(str(Settings().database_url))
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return {row[0] for row in cur.fetchall()}


def _has_user_sports() -> set[str]:
    return _query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'user_sports'"
    )


def _has_onboarded_at() -> set[str]:
    return _query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'onboarded_at'"
    )


def test_upgrade_head_adds_user_sports_and_onboarded_at() -> None:
    cfg = _alembic_config()
    command.downgrade(cfg, _PREVIOUS_REVISION)

    try:
        command.upgrade(cfg, "head")
        assert _has_user_sports() == {"user_sports"}
        assert _has_onboarded_at() == {"onboarded_at"}
    finally:
        command.upgrade(cfg, "head")


def test_downgrade_reverses_the_onboarding_schema_cleanly() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    try:
        command.downgrade(cfg, _PREVIOUS_REVISION)
        assert _has_user_sports() == set()
        assert _has_onboarded_at() == set()
    finally:
        command.upgrade(cfg, "head")


def test_upgrade_head_run_twice_is_a_noop() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    try:
        command.upgrade(cfg, "head")  # must not raise
        assert _has_user_sports() == {"user_sports"}
    finally:
        command.upgrade(cfg, "head")


def test_migration_adds_and_removes_weekly_availability() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    query = (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'weekly_availability'"
    )

    try:
        assert _query(query) == {"weekly_availability"}
        command.downgrade(cfg, "a7c3d91e5b20")
        assert _query(query) == set()
        assert _has_user_sports() == {"user_sports"}
    finally:
        command.upgrade(cfg, "head")


def test_migration_adds_and_removes_goals_with_an_indexed_cascading_user_id() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    table = (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'goals'"
    )
    cascade = (
        "SELECT rc.delete_rule FROM information_schema.referential_constraints rc "
        "JOIN information_schema.table_constraints tc ON tc.constraint_name = rc.constraint_name "
        "WHERE tc.table_name = 'goals'"
    )
    index = (
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'goals' AND indexdef LIKE '%(user_id)'"
    )

    try:
        assert _query(table) == {"goals"}
        assert _query(cascade) == {"CASCADE"}
        assert len(_query(index)) == 1
        command.downgrade(cfg, "c81e4f2a7d16")
        assert _query(table) == set()
        assert _query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'weekly_availability'"
        ) == {"weekly_availability"}
    finally:
        command.upgrade(cfg, "head")


def test_migration_adds_a_nullable_goal_sport_and_keeps_existing_goals_general() -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    column = (
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name = 'goals' AND column_name = 'sport_id'"
    )
    dsn = to_psycopg_dsn(str(Settings().database_url))

    try:
        assert _query(column) == {"YES"}
        command.downgrade(cfg, _GOALS_REVISION)
        assert _query(column) == set()
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, sub, email) "
                "VALUES (gen_random_uuid(), 'sub-goal-migration', 'g@example.com') RETURNING id"
            )
            row = cur.fetchone()
            assert row is not None
            user_id = row[0]
            cur.execute(
                "INSERT INTO goals (id, user_id, position, text) "
                "VALUES (gen_random_uuid(), %s, 0, 'Old goal')",
                (user_id,),
            )
            conn.commit()
        command.upgrade(cfg, "head")
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT sport_id FROM goals WHERE user_id = %s", (user_id,))
            assert cur.fetchall() == [(None,)]
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            conn.commit()
    finally:
        command.upgrade(cfg, "head")
