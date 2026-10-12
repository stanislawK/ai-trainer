"""`ai-trainer purge-conversations` against the real Postgres (ADR-0021, ticket #148). The CLI
opens its own engine and commits, so this writes real rows and cleans up after itself."""

import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text

from ai_trainer import cli
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.settings import Settings
from tests.chat_support import FixedClock

_SUB = "purge-cli-user"
_LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)


@pytest.fixture
async def seeded() -> AsyncIterator[tuple[UUID, UUID]]:
    """A committed user with an ancient conversation and a fresh one; the user, and with it
    whatever the purge left, is removed afterwards. Yields (ancient id, live id)."""
    engine = build_engine(str(Settings().database_url))
    factory = build_session_factory(engine)
    user = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=_SUB, email=f"{_SUB}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    # A retention long enough to keep the live row visible to the repository that creates both.
    repository = SqlAlchemyConversationRepository(factory, clock=FixedClock(), retention_days=36500)
    ancient = await repository.create(user.id, "ancient", _LONG_AGO)
    live = await repository.create(user.id, "live", datetime.now(UTC) + timedelta(minutes=1))
    yield ancient.id, live.id
    async with engine.begin() as connection:
        await connection.execute(text("DELETE FROM users WHERE sub = :sub"), {"sub": _SUB})
    await engine.dispose()


async def _ids() -> set[UUID]:
    engine = build_engine(str(Settings().database_url))
    async with engine.begin() as connection:
        rows = await connection.execute(
            text(
                "SELECT c.id FROM conversations c JOIN users u ON u.id = c.user_id "
                "WHERE u.sub = :sub"
            ),
            {"sub": _SUB},
        )
        found = {row[0] for row in rows}
    await engine.dispose()
    return found


async def test_main_deletes_the_expired_conversation_prints_a_count_and_a_second_run_prints_zero(
    seeded: tuple[UUID, UUID],
) -> None:
    ancient, live = seeded
    first, second = io.StringIO(), io.StringIO()

    await cli.run(["purge-conversations"], output=first)
    after_first = await _ids()
    await cli.run(["purge-conversations"], output=second)

    assert int(first.getvalue()) >= 1
    assert after_first == {live}
    assert second.getvalue() == "0\n"
    assert ancient not in after_first
