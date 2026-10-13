"""Migration `d3a7e5c1b942` (ADR-0022, ADR-0024, ticket #154): the four training-session tables
with every foreign key, CHECK and index ADR-0022 lists, created expand-only, so upgrading from
`main`'s head keeps every existing row as it was."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb

from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.settings import Settings

_PREVIOUS_REVISION = "b6f1c2d9e4a7"
_NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
_TABLES = ("training_sessions", "training_session_sources", "session_drafts", "clarifications")
# Every table already there before this migration, and what identifies a row in it.
_EXISTING = {
    "users": "id",
    "conversations": "id",
    "chat_messages": "id",
    "sessions": "id",
    "user_sports": "user_id, sport_id",
    "weekly_availability": "user_id, weekday",
    "goals": "id",
    "llm_calls": "id",
    "user_status_changes": "id",
}


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _seed_user(cur: psycopg.Cursor[Any]) -> dict[str, UUID]:
    """One athlete with a row in every table that exists at `main`'s head."""
    user, conversation, message = uuid4(), uuid4(), uuid4()
    cur.execute(
        "INSERT INTO users (id, sub, email) VALUES (%s, %s, %s)",
        (user, f"training-{user}", f"{user}@example.com"),
    )
    cur.execute(
        "INSERT INTO conversations (id, user_id, title, last_message_at) VALUES (%s, %s, %s, %s)",
        (conversation, user, "Climbed", _NOW),
    )
    cur.execute(
        "INSERT INTO chat_messages (id, user_id, conversation_id, role, text, created_at)"
        " VALUES (%s, %s, %s, 'user', %s, %s)",
        (message, user, conversation, "I climbed 3 6A", _NOW),
    )
    cur.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (%s, %s, %s)", (uuid4(), user, _NOW)
    )
    cur.execute("INSERT INTO user_sports (user_id, sport_id) VALUES (%s, 'climbing')", (user,))
    cur.execute(
        "INSERT INTO weekly_availability (user_id, weekday, minutes) VALUES (%s, 1, 90)", (user,)
    )
    cur.execute(
        "INSERT INTO goals (id, user_id, position, text) VALUES (%s, %s, 0, 'Send 7a')",
        (uuid4(), user),
    )
    cur.execute(
        "INSERT INTO llm_calls (id, user_id, template_id, template_version, model,"
        " input_tokens, output_tokens, latency_ms, outcome)"
        " VALUES (%s, %s, 'router', 2, 'm', 1, 1, 1, 'success')",
        (uuid4(), user),
    )
    cur.execute(
        "INSERT INTO user_status_changes (id, actor_user_id, target_user_id, old_status,"
        " new_status) VALUES (%s, %s, %s, 'pending', 'active')",
        (uuid4(), user, user),
    )
    return {"user": user, "conversation": conversation, "message": message}


def _snapshot(cur: psycopg.Cursor[Any]) -> dict[str, list[tuple[Any, ...]]]:
    snapshot = {}
    for table, key in _EXISTING.items():
        cur.execute(f"SELECT * FROM {table} ORDER BY {key}")
        snapshot[table] = cur.fetchall()
    return snapshot


@pytest.fixture
def at_previous_revision() -> Iterator[None]:
    """`main`'s head with two athletes' rows in every table; leaves the database at head."""
    cfg = Config("alembic.ini")
    command.downgrade(cfg, _PREVIOUS_REVISION)
    try:
        with _connect() as conn, conn.cursor() as cur:
            _seed_user(cur)
            _seed_user(cur)
        yield
    finally:
        command.upgrade(cfg, "head")


def test_upgrading_with_data_in_every_table_keeps_every_row_and_value(
    at_previous_revision: None,
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        before = _snapshot(cur)

    command.upgrade(Config("alembic.ini"), "head")

    with _connect() as conn, conn.cursor() as cur:
        assert _snapshot(cur) == before
    assert all(before[table] for table in _EXISTING)


def test_downgrade_drops_only_the_four_tables_and_upgrade_restores_them() -> None:
    cfg = Config("alembic.ini")
    try:
        command.downgrade(cfg, _PREVIOUS_REVISION)
        assert _existing_tables() & set(_TABLES) == set()
        command.upgrade(cfg, "head")
        assert _existing_tables() >= set(_TABLES)
    finally:
        command.upgrade(cfg, "head")


def _existing_tables() -> set[str]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in cur.fetchall()}


# (table, columns, referenced table, ON DELETE): `c` is CASCADE, `n` is SET NULL.
_FOREIGN_KEYS = {
    ("training_sessions", ("user_id",), "users", "c"),
    ("training_session_sources", ("user_id",), "users", "c"),
    ("training_session_sources", ("session_id", "user_id"), "training_sessions", "c"),
    ("session_drafts", ("user_id",), "users", "c"),
    ("session_drafts", ("conversation_id", "user_id"), "conversations", "c"),
    ("session_drafts", ("message_id", "user_id"), "chat_messages", "c"),
    ("session_drafts", ("extends_session_id", "user_id"), "training_sessions", "n"),
    ("session_drafts", ("session_id", "user_id"), "training_sessions", "n"),
    ("clarifications", ("user_id",), "users", "c"),
    ("clarifications", ("conversation_id", "user_id"), "conversations", "c"),
    ("clarifications", ("message_id", "user_id"), "chat_messages", "c"),
    ("clarifications", ("draft_id", "user_id"), "session_drafts", "c"),
}


def test_every_foreign_key_has_the_on_delete_adr_0022_states() -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT c.conrelid::regclass::text,"
            " array(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(n, i)"
            "  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.n ORDER BY k.i),"
            " c.confrelid::regclass::text, c.confdeltype"
            " FROM pg_constraint c WHERE c.contype = 'f' AND c.conrelid::regclass::text = ANY(%s)",
            (list(_TABLES),),
        )
        found = {(table, tuple(cols), target, rule) for table, cols, target, rule in cur.fetchall()}
    assert found == _FOREIGN_KEYS


_INDEXES = {
    "training_sessions": {
        "ix_training_sessions_user_id_occurred_on": "(user_id, occurred_on DESC)",
        # The target of the composite keys that keep references inside one user.
        "training_sessions_id_user_id_key": "(id, user_id)",
        "ix_training_sessions_user_id_sport_id_occurred_on": (
            "(user_id, sport_id, occurred_on DESC)"
        ),
    },
    "training_session_sources": {
        "ix_training_session_sources_user_id": "(user_id)",
        "ix_training_session_sources_session_id_logged_at": "(session_id, logged_at)",
    },
    "session_drafts": {
        "ix_session_drafts_user_id_conversation_id_status": "(user_id, conversation_id, status)",
        "ix_session_drafts_message_id": "(message_id)",
        "session_drafts_id_user_id_key": "(id, user_id)",
    },
    "clarifications": {
        "ix_clarifications_user_id_conversation_id_status": "(user_id, conversation_id, status)",
        "ix_clarifications_message_id": "(message_id)",
        "ix_clarifications_draft_id": "(draft_id)",
    },
}


@pytest.mark.parametrize("table", _TABLES)
def test_each_table_has_the_indexes_adr_0022_lists(table: str) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = %s AND indexname <> %s",
            (table, f"{table}_pkey"),
        )
        found = {name: definition for name, definition in cur.fetchall()}
    assert set(found) == set(_INDEXES[table])
    for name, columns in _INDEXES[table].items():
        assert found[name].endswith(f"USING btree {columns}")


@pytest.fixture
def athlete() -> Iterator[dict[str, UUID]]:
    with _connect() as conn, conn.cursor() as cur:
        seeded = _seed_user(cur)
    yield seeded
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = %s", (seeded["user"],))


def _insert_session(cur: psycopg.Cursor[Any], user: UUID, **overrides: Any) -> UUID:
    row: dict[str, Any] = {
        "id": uuid4(),
        "user_id": user,
        "sport_id": "climbing",
        "occurred_on": _NOW.date(),
        "timezone": "Europe/Warsaw",
        "duration_min": None,
        "rpe": None,
        "feeling": None,
        "notes": None,
        "payload": Jsonb({"sport": "climbing", "activities": []}),
        "payload_version": 1,
        "created_at": _NOW,
        "updated_at": _NOW,
    } | overrides
    columns = ", ".join(row)
    cur.execute(
        f"INSERT INTO training_sessions ({columns}) VALUES ({', '.join(['%s'] * len(row))})",
        tuple(row.values()),
    )
    return UUID(str(row["id"]))


@pytest.mark.parametrize(
    "overrides",
    [
        {"duration_min": 0},
        {"duration_min": 1441},
        {"rpe": 0},
        {"rpe": 11},
        {"feeling": "x" * 201},
        {"notes": "x" * 2001},
        {"payload": Jsonb([1, 2])},
        {"payload_version": 0},
    ],
)
def test_training_sessions_rejects_values_outside_its_checks(
    athlete: dict[str, UUID], overrides: dict[str, Any]
) -> None:
    with _connect() as conn, conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        _insert_session(cur, athlete["user"], **overrides)


def test_training_sessions_accepts_values_at_the_edges_of_its_checks(
    athlete: dict[str, UUID],
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        for overrides in (
            {"duration_min": 1, "rpe": 1},
            {"duration_min": 1440, "rpe": 10, "feeling": "x" * 200, "notes": "x" * 2000},
        ):
            _insert_session(cur, athlete["user"], **overrides)


def test_a_source_must_come_from_chat_or_manual(athlete: dict[str, UUID]) -> None:
    with _connect() as conn, conn.cursor() as cur:
        session = _insert_session(cur, athlete["user"])
        insert = (
            "INSERT INTO training_session_sources (id, user_id, session_id, raw_text, source,"
            " logged_at) VALUES (%s, %s, %s, 'I climbed', %s, %s)"
        )
        for source in ("chat", "manual"):
            cur.execute(insert, (uuid4(), athlete["user"], session, source, _NOW))
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(insert, (uuid4(), athlete["user"], session, "import", _NOW))


def _insert_draft(cur: psycopg.Cursor[Any], athlete: dict[str, UUID], status: str) -> UUID:
    draft_id = uuid4()
    cur.execute(
        "INSERT INTO session_drafts (id, user_id, conversation_id, message_id, sport_id, draft,"
        " payload_version, status, created_at) VALUES (%s, %s, %s, %s, 'climbing', %s, 1, %s, %s)",
        (
            draft_id,
            athlete["user"],
            athlete["conversation"],
            athlete["message"],
            Jsonb({}),
            status,
            _NOW,
        ),
    )
    return draft_id


def test_a_draft_status_is_pending_confirmed_or_discarded(athlete: dict[str, UUID]) -> None:
    with _connect() as conn, conn.cursor() as cur:
        for status in ("pending", "confirmed", "discarded"):
            _insert_draft(cur, athlete, status)
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_draft(cur, athlete, "saved")


def test_a_draft_cannot_join_another_users_conversation(athlete: dict[str, UUID]) -> None:
    with _connect() as conn, conn.cursor() as cur:
        other = _seed_user(cur)
        stranger = {**athlete, "conversation": other["conversation"]}
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert_draft(cur, stranger, "pending")


def test_a_clarification_status_is_open_answered_or_abandoned(athlete: dict[str, UUID]) -> None:
    insert = (
        "INSERT INTO clarifications (id, user_id, conversation_id, message_id, purpose,"
        " clarification, status, created_at) VALUES (%s, %s, %s, %s, 'sport', %s, %s, %s)"
    )
    with _connect() as conn, conn.cursor() as cur:
        for status in ("open", "answered", "abandoned"):
            cur.execute(
                insert,
                (
                    uuid4(),
                    athlete["user"],
                    athlete["conversation"],
                    athlete["message"],
                    Jsonb({}),
                    status,
                    _NOW,
                ),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                insert,
                (
                    uuid4(),
                    athlete["user"],
                    athlete["conversation"],
                    athlete["message"],
                    Jsonb({}),
                    "closed",
                    _NOW,
                ),
            )


def test_deleting_a_session_keeps_its_drafts_with_their_links_cleared(
    athlete: dict[str, UUID],
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        session = _insert_session(cur, athlete["user"])
        draft = _insert_draft(cur, athlete, "confirmed")
        cur.execute(
            "UPDATE session_drafts SET session_id = %s, extends_session_id = %s WHERE id = %s",
            (session, session, draft),
        )
        cur.execute("DELETE FROM training_sessions WHERE id = %s", (session,))
        cur.execute(
            "SELECT session_id, extends_session_id FROM session_drafts WHERE id = %s", (draft,)
        )
        assert cur.fetchone() == (None, None)


# Tenancy (ADR-0022 checklist, ADR-0004 invariant 2): every reference to a user-owned row is
# keyed with `user_id`, so no row of one athlete can point at another athlete's data.


def _insert_source(cur: psycopg.Cursor[Any], user: UUID, session: UUID) -> None:
    cur.execute(
        "INSERT INTO training_session_sources (id, user_id, session_id, raw_text, source,"
        " logged_at) VALUES (%s, %s, %s, 'I climbed', 'chat', %s)",
        (uuid4(), user, session, _NOW),
    )


def _insert_clarification(
    cur: psycopg.Cursor[Any], athlete: dict[str, UUID], draft: UUID | None = None
) -> None:
    cur.execute(
        "INSERT INTO clarifications (id, user_id, conversation_id, message_id, draft_id, purpose,"
        " clarification, status, created_at) VALUES (%s, %s, %s, %s, %s, 'sport', %s, 'open', %s)",
        (
            uuid4(),
            athlete["user"],
            athlete["conversation"],
            athlete["message"],
            draft,
            Jsonb({}),
            _NOW,
        ),
    )


def test_a_source_cannot_join_another_users_session(athlete: dict[str, UUID]) -> None:
    with _connect() as conn, conn.cursor() as cur:
        other = _seed_user(cur)
        theirs = _insert_session(cur, other["user"])
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert_source(cur, athlete["user"], theirs)


def test_a_draft_cannot_come_from_another_users_message(athlete: dict[str, UUID]) -> None:
    with _connect() as conn, conn.cursor() as cur:
        other = _seed_user(cur)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert_draft(cur, {**athlete, "message": other["message"]}, "pending")


@pytest.mark.parametrize("column", ["session_id", "extends_session_id"])
def test_a_draft_cannot_point_at_another_users_session(
    athlete: dict[str, UUID], column: str
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        other = _seed_user(cur)
        theirs = _insert_session(cur, other["user"])
        draft = _insert_draft(cur, athlete, "pending")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(f"UPDATE session_drafts SET {column} = %s WHERE id = %s", (theirs, draft))


def test_a_clarification_cannot_join_another_users_conversation_message_or_draft(
    athlete: dict[str, UUID],
) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            other = _seed_user(cur)
            their_draft = _insert_draft(cur, other, "pending")
            _insert_clarification(cur, athlete)
        for stranger, draft in (
            ({**athlete, "conversation": other["conversation"]}, None),
            ({**athlete, "message": other["message"]}, None),
            (athlete, their_draft),
        ):
            with (
                pytest.raises(psycopg.errors.ForeignKeyViolation),
                conn.transaction(),
                conn.cursor() as cur,
            ):
                _insert_clarification(cur, stranger, draft)


def test_deleting_one_athlete_leaves_every_training_row_of_another(
    athlete: dict[str, UUID],
) -> None:
    with _connect() as conn, conn.cursor() as cur:
        session = _insert_session(cur, athlete["user"])
        _insert_source(cur, athlete["user"], session)
        draft = _insert_draft(cur, athlete, "pending")
        _insert_clarification(cur, athlete, draft)
        other = _seed_user(cur)
        their_session = _insert_session(cur, other["user"])
        _insert_source(cur, other["user"], their_session)
        _insert_clarification(cur, other, _insert_draft(cur, other, "pending"))

        cur.execute("DELETE FROM users WHERE id = %s", (other["user"],))

        counts = []
        for table in _TABLES:
            cur.execute(f"SELECT count(*) FROM {table} WHERE user_id = %s", (athlete["user"],))
            row = cur.fetchone()
            assert row is not None
            counts.append(row[0])
    assert counts == [1, 1, 1, 1]
