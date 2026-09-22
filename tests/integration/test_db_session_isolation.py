from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_INSERT = text("INSERT INTO isolation_probe (id) VALUES (:id)")
_COUNT = text("SELECT count(*) FROM isolation_probe WHERE id = :id")


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
