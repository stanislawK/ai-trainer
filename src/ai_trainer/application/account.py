from uuid import UUID

from ai_trainer.application.ports.users import UsersRepositoryPort


async def delete_account(user_id: UUID, *, users: UsersRepositoryPort) -> None:
    """Deletes the user's row (G7). Cascades remove their sessions and every other
    user-owned row (ADR-0004 invariant 1); nothing else needs deleting here."""
    await users.delete(user_id)
