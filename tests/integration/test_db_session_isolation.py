import asyncio
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.db import build_engine
from ai_trainer.settings import Settings

_INSERT = text("INSERT INTO isolation_probe (id) VALUES (:id)")
_COUNT = text("SELECT count(*) FROM isolation_probe WHERE id = :id")


@pytest.fixture(scope="module", autouse=True)
def _isolation_probe_table() -> Iterator[None]:
    """A table that outlives any single test, used to prove per-test isolation.

    Scoped to this module, not the session (#328): under xdist every worker runs its own
    session, so a session-wide table was created by each and dropped by whichever finished
    first, while another could still be using it.
    """
    asyncio.run(_execute("CREATE TABLE IF NOT EXISTS isolation_probe (id INTEGER PRIMARY KEY)"))
    yield
    asyncio.run(_execute("DROP TABLE IF EXISTS isolation_probe"))


async def _execute(statement: str) -> None:
    engine = build_engine(str(Settings().database_url))
    async with engine.begin() as connection:
        await connection.execute(text(statement))
    await engine.dispose()


async def test_first_test_writes_row_seven_and_commits(db_session: AsyncSession) -> None:
    await db_session.execute(_INSERT, {"id": 7})
    await db_session.commit()

    result = await db_session.execute(_COUNT, {"id": 7})
    assert result.scalar_one() == 1


async def test_second_test_writes_the_same_row_id_without_conflict(
    db_session: AsyncSession,
) -> None:
    """If the first test's rollback ever failed, this insert would hit a
    duplicate primary key instead of succeeding."""
    await db_session.execute(_INSERT, {"id": 7})
    await db_session.commit()

    result = await db_session.execute(_COUNT, {"id": 7})
    assert result.scalar_one() == 1
