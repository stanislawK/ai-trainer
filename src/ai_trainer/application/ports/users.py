from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.users import NewUser, User


class UsersRepositoryPort(Protocol):
    """Persists `users` rows (ADR-0005)."""

    async def get_by_sub(self, sub: str) -> User | None: ...

    async def get(self, user_id: UUID) -> User | None: ...

    async def create(self, new_user: NewUser) -> User:
        """Raises `UserAlreadyExistsError` if a concurrent sign-in already took this `sub`."""
        ...

    async def list_all(self) -> Sequence[User]:
        """Every user, for the admin page (F7)."""
        ...

    async def delete(self, user_id: UUID) -> None:
        """Deletes the user row, no-op if it's already gone. Cascades remove every
        user-owned row (ADR-0004 invariant 1, G7)."""
        ...
