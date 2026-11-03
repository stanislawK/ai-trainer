"""An in-memory `AdminTokensRepositoryPort` and the `AdminTokens` service built on it, for tests
that need the admin router without PostgreSQL (#455)."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from ai_trainer.application.admin_tokens import AdminTokens
from ai_trainer.domain.admin_tokens import AdminToken, NewAdminToken, TokenState


class MemoryAdminTokens:
    def __init__(self) -> None:
        self.rows: dict[UUID, NewAdminToken] = {}
        self.revoked: dict[UUID, datetime] = {}

    def _token(self, row: NewAdminToken) -> AdminToken:
        return AdminToken(
            id=row.id,
            user_id=row.user_id,
            display_prefix=row.display_prefix,
            created_at=row.created_at,
            expires_at=row.expires_at,
            revoked_at=self.revoked.get(row.id),
        )

    async def add(self, token: NewAdminToken) -> AdminToken:
        self.rows[token.id] = token
        return self._token(token)

    async def list_for_user(self, user_id: UUID) -> Sequence[AdminToken]:
        # Newest first, then by id, as the SQL adapter orders them.
        mine = sorted(
            (self._token(row) for row in self.rows.values() if row.user_id == user_id),
            key=lambda token: token.id,
        )
        return sorted(mine, key=lambda token: token.created_at, reverse=True)

    async def revoke(self, user_id: UUID, token_id: UUID, *, at: datetime) -> AdminToken | None:
        row = self.rows.get(token_id)
        if row is None or row.user_id != user_id:
            return None
        if self._token(row).state_at(at) is TokenState.ACTIVE:
            self.revoked[token_id] = at
        return self._token(row)


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def memory_admin_tokens() -> AdminTokens:
    """`build_admin_router`'s `tokens`, for a test about the rest of the admin view."""
    return AdminTokens(tokens=MemoryAdminTokens(), clock=_FixedClock())
