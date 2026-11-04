from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.admin_tokens import AdminToken, NewAdminToken


class AdminTokensRepositoryPort(Protocol):
    """An admin's own tokens (ADR-0036). Every method but `get_by_hash` is scoped by the
    admin's `user_id`; that one is how a request finds whose token it carries."""

    async def add(self, token: NewAdminToken) -> AdminToken: ...

    async def list_for_user(self, user_id: UUID) -> Sequence[AdminToken]:
        """Newest first."""
        ...

    async def revoke(self, user_id: UUID, token_id: UUID, *, at: datetime) -> AdminToken | None:
        """Sets `revoked_at` on the user's token if it is still active at `at`, and returns the
        token either way. None when the user has no such token."""
        ...

    async def get_by_hash(self, token_hash: str) -> AdminToken | None:
        """The token with this hash, whoever owns it, in any state: the authentication lookup
        (#457), which can't be scoped, since the token is what names the user."""
        ...

    async def touch(self, user_id: UUID, token_id: UUID, *, at: datetime) -> None:
        """Sets `last_used_at` on the user's own token; does nothing for anyone else's."""
        ...
