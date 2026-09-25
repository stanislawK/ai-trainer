from typing import Protocol
from uuid import UUID

from ai_trainer.domain.users import User, UserStatus


class UserStatusChangerPort(Protocol):
    """Atomically updates a user's status and records the change (ADR-0005 invariant 9): the
    status update and its audit row commit in one transaction, so a crash between them can
    never leave a status change with no audit row."""

    async def change(
        self, *, target_user_id: UUID, new_status: UserStatus, actor_user_id: UUID
    ) -> tuple[User, UserStatus]:
        """Returns the updated user and their status before the change. Raises
        `UserNotFoundError` if `target_user_id` doesn't exist."""
        ...
