from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.admin_tokens import AdminToken, NewAdminToken


class AdminTokensRepositoryPort(Protocol):
    """An admin's own tokens (ADR-0036): every method is scoped by the admin's `user_id`."""

    async def add(self, token: NewAdminToken) -> AdminToken: ...

    async def list_for_user(self, user_id: UUID) -> Sequence[AdminToken]:
        """Newest first."""
        ...

    async def revoke(self, user_id: UUID, token_id: UUID, *, at: datetime) -> AdminToken | None:
        """Sets `revoked_at` on the user's token if it is still active at `at`, and returns the
        token either way. None when the user has no such token."""
        ...
