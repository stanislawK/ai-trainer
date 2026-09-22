from typing import Protocol

from ai_trainer.domain.users import NewUser, User


class UsersRepositoryPort(Protocol):
    """Persists `users` rows (ADR-0005)."""

    async def get_by_sub(self, sub: str) -> User | None: ...

    async def create(self, new_user: NewUser) -> User:
        """Raises `UserAlreadyExistsError` if a concurrent sign-in already took this `sub`."""
        ...
