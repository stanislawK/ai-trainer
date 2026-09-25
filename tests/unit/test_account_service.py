from datetime import UTC, datetime
from uuid import UUID, uuid4

from ai_trainer.application.account import delete_account
from ai_trainer.domain.users import NewUser, User, UserStatus

FROZEN_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


class FakeUsersRepository:
    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.delete_calls: list[UUID] = []

    async def get_by_sub(self, sub: str) -> User | None:
        raise NotImplementedError

    async def get(self, user_id: UUID) -> User | None:
        return self.users.get(user_id)

    async def create(self, new_user: NewUser) -> User:
        user = User(id=uuid4(), created_at=FROZEN_NOW, **new_user.model_dump())
        self.users[user.id] = user
        return user

    async def list_all(self) -> list[User]:
        raise NotImplementedError

    async def delete(self, user_id: UUID) -> None:
        self.delete_calls.append(user_id)
        self.users.pop(user_id, None)


async def test_delete_account_deletes_the_user_row_by_id() -> None:
    users = FakeUsersRepository()
    user = await users.create(
        NewUser(
            sub="google-sub-1",
            email="athlete@example.com",
            name="Athlete",
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )

    await delete_account(user.id, users=users)

    assert users.delete_calls == [user.id]
    assert await users.get(user.id) is None
