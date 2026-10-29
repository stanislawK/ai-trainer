"""Migration `b6f1c2d9e4a7` (ADR-0021, ADR-0024, ticket #146): upgrading from `main`'s head with
chat rows in place loses none of them, and puts each user's messages in one backfilled
conversation, titled from its first athlete message the way the app titles one."""

import asyncio
import importlib.util
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.health import to_psycopg_dsn
from ai_trainer.application.chat import HISTORY_LIMIT, list_history
from ai_trainer.domain.chat import conversation_title
from ai_trainer.settings import Settings
from tests.chat_support import FixedClock

_PREVIOUS_REVISION = "9a4c6e2b8d13"
_START = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
_MESSAGE_COLUMNS = "id, user_id, role, text, sports, template_id, template_version, created_at"


_MIGRATION = Path("migrations/versions/b6f1c2d9e4a7_add_conversations.py")


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("add_conversations", _MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(to_psycopg_dsn(str(Settings().database_url)))


def _add_user(cur: psycopg.Cursor[Any], sub: str) -> UUID:
    # The scratch database lives for the module, so each fixture run needs fresh subs.
    user_id = uuid4()
    cur.execute(
        "INSERT INTO users (id, sub, email) VALUES (%s, %s, %s)",
        (user_id, f"{sub}-{user_id}", f"{sub}@example.com"),
    )
    return user_id


def _add_message(
    cur: psycopg.Cursor[Any],
    user_id: UUID,
    role: str,
    text: str,
    minutes: int,
    *,
    sports: list[str] | None = None,
    template: tuple[str, int] | None = None,
) -> None:
    cur.execute(
        "INSERT INTO chat_messages"
        " (id, user_id, role, text, sports, template_id, template_version, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            uuid4(),
            user_id,
            role,
            text,
            sports,
            template[0] if template else None,
            template[1] if template else None,
            _START + timedelta(minutes=minutes),
        ),
    )


@pytest.fixture
def seeded() -> Iterator[dict[str, UUID]]:
    """`main`'s head with chat rows for two athletes, one athlete who never chatted, and one
    whose only row is a reply. Leaves the scratch database at head for the next test."""
    cfg = Config("alembic.ini")
    command.downgrade(cfg, _PREVIOUS_REVISION)
    try:
        with _connect() as conn, conn.cursor() as cur:
            ada = _add_user(cur, "migrate-ada")
            bo = _add_user(cur, "migrate-bo")
            quiet = _add_user(cur, "migrate-quiet")
            replies_only = _add_user(cur, "migrate-replies-only")
            _add_message(cur, ada, "user", "  Rode \t 90 km\n\nwith hills  ", 0)
            _add_message(
                cur,
                ada,
                "assistant",
                "Not yet!",
                1,
                sports=["cycling"],
                template=("not_yet", 1),
            )
            _add_message(cur, ada, "user", "And 2 more of those", 30)
            _add_message(cur, bo, "assistant", "Hello!", 4)
            _add_message(cur, bo, "user", "word " * 20, 5)
            _add_message(cur, replies_only, "assistant", "Orphan reply", 7)
        yield {"ada": ada, "bo": bo, "quiet": quiet, "replies_only": replies_only}
    finally:
        command.upgrade(cfg, "head")


def _messages(cur: psycopg.Cursor[Any]) -> list[tuple[Any, ...]]:
    cur.execute(f"SELECT {_MESSAGE_COLUMNS} FROM chat_messages ORDER BY id")
    return cur.fetchall()


def test_upgrading_with_chat_rows_keeps_every_row_and_value(seeded: dict[str, UUID]) -> None:
    with _connect() as conn, conn.cursor() as cur:
        before = _messages(cur)

    command.upgrade(Config("alembic.ini"), "head")

    with _connect() as conn, conn.cursor() as cur:
        assert _messages(cur) == before
    assert len([row for row in before if row[1] in set(seeded.values())]) == 6


def test_each_user_with_messages_gets_one_conversation_holding_all_of_them(
    seeded: dict[str, UUID],
) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT c.user_id, count(DISTINCT c.id), count(m.id)"
            " FROM conversations c JOIN chat_messages m ON m.conversation_id = c.id"
            " GROUP BY c.user_id"
        )
        # Earlier tests in this module seeded users of their own; only this run's count.
        mine = set(seeded.values())
        per_user = {row[0]: (row[1], row[2]) for row in cur.fetchall() if row[0] in mine}
        cur.execute(
            "SELECT count(*) FROM chat_messages m JOIN conversations c"
            " ON c.id = m.conversation_id WHERE c.user_id <> m.user_id"
        )
        crossed = cur.fetchone()

    assert per_user == {
        seeded["ada"]: (1, 3),
        seeded["bo"]: (1, 2),
        seeded["replies_only"]: (1, 1),
    }
    assert seeded["quiet"] not in per_user
    assert crossed == (0,)


def test_a_backfilled_conversation_is_titled_and_timed_from_its_messages(
    seeded: dict[str, UUID],
) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, title, created_at, last_message_at FROM conversations")
        rows = {row[0]: row[1:] for row in cur.fetchall()}

    assert rows[seeded["ada"]] == (
        conversation_title("  Rode \t 90 km\n\nwith hills  "),
        _START,
        _START + timedelta(minutes=30),
    )
    assert rows[seeded["ada"]][0] == "Rode 90 km with hills"
    # The title is the first athlete message, even when a reply came first.
    assert rows[seeded["bo"]] == (
        conversation_title("word " * 20),
        _START + timedelta(minutes=4),
        _START + timedelta(minutes=5),
    )
    assert rows[seeded["replies_only"]][0] is None


def test_conversation_id_is_required_after_the_upgrade(seeded: dict[str, UUID]) -> None:
    command.upgrade(Config("alembic.ini"), "head")

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns"
            " WHERE table_name = 'chat_messages' AND column_name = 'conversation_id'"
        )
        nullable = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM pg_constraint"
            " WHERE conrelid = 'chat_messages'::regclass AND contype = 'c'"
            " AND conname = 'chat_messages_conversation_id_not_null'"
        )
        leftover_check = cur.fetchone()

    assert nullable == ("NO",)
    assert leftover_check == (0,)


def test_downgrade_removes_conversations_and_upgrade_restores_them() -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    try:
        command.downgrade(cfg, _PREVIOUS_REVISION)
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.conversations')")
            assert cur.fetchone() == (None,)
    finally:
        command.upgrade(cfg, "head")


async def _shown_at_root(athlete: UUID) -> list[UUID]:
    """The IDs `/` lists for `athlete`: the latest conversation's history, through the app's
    own repositories."""
    engine = build_engine(str(Settings().database_url))
    try:
        factory = build_session_factory(engine)
        [latest] = await SqlAlchemyConversationRepository(
            factory, clock=FixedClock(), retention_days=14
        ).list_recent(athlete, 1)
        history = await list_history(
            athlete, latest.id, repository=SqlAlchemyChatRepository(factory)
        )
        return [message.id for message in history]
    finally:
        await engine.dispose()


def test_root_shows_the_same_history_after_the_upgrade() -> None:
    """The messages `/` showed on `main` (a user's latest 50, oldest first) are exactly what it
    shows after the upgrade, read through the app's own repositories (#146 AC 1)."""
    cfg = Config("alembic.ini")
    command.downgrade(cfg, _PREVIOUS_REVISION)
    try:
        with _connect() as conn, conn.cursor() as cur:
            athlete = _add_user(cur, "migrate-root")
            for minute in range(HISTORY_LIMIT + 5):
                role = "user" if minute % 2 == 0 else "assistant"
                _add_message(cur, athlete, role, f"turn {minute}", minute)
            cur.execute(
                "SELECT id FROM chat_messages WHERE user_id = %s"
                " ORDER BY created_at DESC, id DESC LIMIT %s",
                (athlete, HISTORY_LIMIT),
            )
            shown_on_main = [row[0] for row in reversed(cur.fetchall())]
    finally:
        command.upgrade(cfg, "head")

    # Sync, with its own loop: Alembic's env.py runs the upgrade under `asyncio.run`.
    assert asyncio.run(_shown_at_root(athlete)) == shown_on_main


def test_a_message_cannot_join_another_users_conversation() -> None:
    """ADR-0021 invariant 1 holds in the schema, not only in the application layer."""
    command.upgrade(Config("alembic.ini"), "head")
    with _connect() as conn, conn.cursor() as cur:
        owner = _add_user(cur, "migrate-owner")
        intruder = _add_user(cur, "migrate-intruder")
        conversation = uuid4()
        cur.execute(
            "INSERT INTO conversations (id, user_id, last_message_at) VALUES (%s, %s, now())",
            (conversation, owner),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                "INSERT INTO chat_messages (id, user_id, conversation_id, role, text)"
                " VALUES (%s, %s, %s, 'user', 'not yours')",
                (uuid4(), intruder, conversation),
            )
        conn.rollback()


_WHITESPACE = [chr(code) for code in range(0x110000) if chr(code).isspace()]


@pytest.mark.parametrize(
    "text",
    [
        *(f"{space}a{space}{space}b{space}" for space in _WHITESPACE),
        "zero\u200bwidth stays",
        "é" * 59 + " 🧗 climb",
        "  " + "x" * 59 + " y",
    ],
    ids=lambda text: "-".join(f"{ord(char):x}" for char in text[:3]),
)
def test_the_backfill_titles_text_exactly_as_the_app_does(text: str) -> None:
    """The backfill's SQL title and `conversation_title` agree on every character Python
    counts as whitespace, whatever the database's collation."""
    title_sql = _migration().title_sql("%s")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {title_sql}", (text,))
        row = cur.fetchone()

    assert row == (conversation_title(text),)
