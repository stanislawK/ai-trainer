import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.db import build_engine
from ai_trainer.settings import Settings


def _database_url() -> str:
    return str(Settings().database_url)


@pytest.fixture(scope="session", autouse=True)
def _isolation_probe_table() -> Iterator[None]:
    """A table that outlives any single test, used to prove per-test isolation.

    Deliberately has no `vector` column: `test_migrations.py` drops and
    recreates the `vector` extension within the same test session, which
    would fail with a dependency error if a persistent table still used it.
    The VECTOR round-trip test instead creates its own scratch table inside
    its own rolled-back transaction (see `test_vector_round_trip.py`).
    """

    async def _create() -> None:
        engine = build_engine(_database_url())
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE IF NOT EXISTS isolation_probe (id INTEGER PRIMARY KEY)")
            )
        await engine.dispose()

    async def _drop() -> None:
        engine = build_engine(_database_url())
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS isolation_probe"))
        await engine.dispose()

    asyncio.run(_create())
    yield
    asyncio.run(_drop())


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Isolates a test in its own transaction, rolled back at teardown.

    SQLAlchemy's "join a session into an external transaction" pattern
    (ADR-0013): the session uses SAVEPOINTs internally, so a test may call
    `commit()` freely, but nothing it does is ever visible outside the test.
    """
    engine = build_engine(_database_url())
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint")
            try:
                yield session
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()
