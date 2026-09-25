from collections.abc import Callable
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.users import NewUser, UserAlreadyExistsError, UserStatus


def _new_user(**overrides: object) -> NewUser:
    defaults: dict[str, object] = {
        "sub": "google-sub-1",
        "email": "athlete@example.com",
        "name": "Athlete",
        "locale": "en",
        "status": UserStatus.PENDING,
    }
    defaults.update(overrides)
    return NewUser(**defaults)  # type: ignore[arg-type]


async def test_create_persists_a_row_with_id_and_created_at(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyUsersRepository(db_session_factory)

    user = await repository.create(_new_user())

    assert user.id is not None
    assert user.created_at is not None
    assert user.sub == "google-sub-1"
    assert user.status is UserStatus.PENDING


async def test_get_by_sub_finds_an_existing_user(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyUsersRepository(db_session_factory)
    created = await repository.create(_new_user(sub="google-sub-2"))

    found = await repository.get_by_sub("google-sub-2")

    assert found is not None
    assert found.id == created.id


async def test_get_by_sub_with_no_match_returns_none(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyUsersRepository(db_session_factory)

    assert await repository.get_by_sub("no-such-sub") is None


async def test_get_finds_an_existing_user_by_id(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyUsersRepository(db_session_factory)
    created = await repository.create(_new_user(sub="google-sub-4"))

    found = await repository.get(created.id)

    assert found is not None
    assert found.sub == "google-sub-4"


async def test_get_with_no_match_returns_none(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyUsersRepository(db_session_factory)

    assert await repository.get(uuid4()) is None


async def test_create_with_a_sub_already_taken_raises_user_already_exists(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """The unique index on `sub` is the real guard against a concurrent-sign-in race
    (ADR-0005); this proves the violation surfaces as `UserAlreadyExistsError`, not a raw,
    unhandled `IntegrityError`."""
    repository = SqlAlchemyUsersRepository(db_session_factory)
    await repository.create(_new_user(sub="google-sub-3"))

    with pytest.raises(UserAlreadyExistsError) as excinfo:
        await repository.create(_new_user(sub="google-sub-3", email="other@example.com"))

    assert excinfo.value.sub == "google-sub-3"


async def test_list_all_returns_every_user(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyUsersRepository(db_session_factory)
    first = await repository.create(_new_user(sub="google-sub-list-1"))
    second = await repository.create(_new_user(sub="google-sub-list-2", status=UserStatus.ACTIVE))

    users = await repository.list_all()

    ids = {user.id for user in users}
    assert first.id in ids
    assert second.id in ids
