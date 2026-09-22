from typing import Protocol
from uuid import UUID

from ai_trainer.domain.sessions import NewSession, Session


class SessionsRepositoryPort(Protocol):
    """Persists `sessions` rows: the PostgreSQL-backed login session (ADR-0005)."""

    async def create(self, new_session: NewSession) -> Session: ...

    async def get(self, session_id: UUID) -> Session | None: ...

    async def delete(self, session_id: UUID) -> None: ...
