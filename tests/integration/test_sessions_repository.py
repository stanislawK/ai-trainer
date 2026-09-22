from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.domain.sessions import NewSession

_INSERT_USER = text("INSERT INTO users (id, sub, email) VALUES (:id, :sub, :email)")


async def _create_user(session_factory: Callable[[], AsyncSession]) -> UUID:
    user_id = uuid4()
    async with session_factory() as session:
        await session.execute(
            _INSERT_USER, {"id": user_id, "sub": str(user_id), "email": f"{user_id}@example.com"}
        )
        await session.commit()
    return user_id


async def test_create_persists_a_row_with_id_and_created_at(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _create_user(db_session_factory)
    repository = SqlAlchemySessionsRepository(db_session_factory)
    expires_at = datetime(2026, 10, 1, tzinfo=UTC)

    session = await repository.create(NewSession(user_id=user_id, expires_at=expires_at))

    assert session.id is not None
    assert session.created_at is not None
    assert session.user_id == user_id
    assert session.expires_at == expires_at


async def test_get_finds_an_existing_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _create_user(db_session_factory)
    repository = SqlAlchemySessionsRepository(db_session_factory)
    created = await repository.create(
        NewSession(user_id=user_id, expires_at=datetime.now(UTC) + timedelta(days=14))
    )

    found = await repository.get(created.id)

    assert found is not None
    assert found.id == created.id


async def test_get_with_no_match_returns_none(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemySessionsRepository(db_session_factory)

    assert await repository.get(uuid4()) is None


async def test_delete_removes_the_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _create_user(db_session_factory)
    repository = SqlAlchemySessionsRepository(db_session_factory)
    created = await repository.create(
        NewSession(user_id=user_id, expires_at=datetime.now(UTC) + timedelta(days=14))
    )

    await repository.delete(created.id)

    assert await repository.get(created.id) is None


async def test_delete_with_no_match_is_a_noop(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemySessionsRepository(db_session_factory)

    await repository.delete(uuid4())  # must not raise
