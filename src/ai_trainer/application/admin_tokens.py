"""Issuing, listing and revoking an admin's tokens (ADR-0036, #455).

The token's text exists only in the `IssuedToken` handed back to the route, which shows it once;
the repository gets its hash and display prefix. The token is never logged.
"""

import base64
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from ai_trainer.application.ports.admin_tokens import AdminTokensRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.domain.admin_tokens import (
    DISPLAY_PREFIX_LENGTH,
    MAX_TOKEN_DAYS,
    TOKEN_BYTES,
    TOKEN_PREFIX,
    AdminToken,
    InvalidTokenLifetimeError,
    NewAdminToken,
    token_hash,
)


@dataclass(frozen=True)
class IssuedToken:
    token: AdminToken
    # Shown once, then gone: never stored, logged or rendered again.
    secret: str

    def __repr__(self) -> str:
        return f"IssuedToken(token={self.token!r}, secret=<hidden>)"


class AdminTokens:
    def __init__(
        self,
        *,
        tokens: AdminTokensRepositoryPort,
        clock: ClockPort,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._tokens = tokens
        self._clock = clock
        self._random_bytes = random_bytes

    def now(self) -> datetime:
        return self._clock.now()

    async def issue(self, user_id: UUID, *, days: int) -> IssuedToken:
        """Raises `InvalidTokenLifetimeError` for a lifetime outside 1 to 30 days."""
        if not 1 <= days <= MAX_TOKEN_DAYS:
            raise InvalidTokenLifetimeError(days)
        random_part = base64.urlsafe_b64encode(self._random_bytes(TOKEN_BYTES)).rstrip(b"=")
        secret = TOKEN_PREFIX + random_part.decode()
        created_at = self._clock.now()
        token = await self._tokens.add(
            NewAdminToken(
                id=uuid4(),
                user_id=user_id,
                token_hash=token_hash(secret),
                display_prefix=secret[:DISPLAY_PREFIX_LENGTH],
                created_at=created_at,
                expires_at=created_at + timedelta(days=days),
            )
        )
        return IssuedToken(token=token, secret=secret)

    async def list(self, user_id: UUID) -> Sequence[AdminToken]:
        return await self._tokens.list_for_user(user_id)

    async def revoke(self, user_id: UUID, token_id: UUID) -> AdminToken | None:
        """None when the user has no such token; an expired or revoked one comes back as is."""
        return await self._tokens.revoke(user_id, token_id, at=self._clock.now())
