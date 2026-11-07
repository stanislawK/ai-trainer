from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.history import StoredGymSession


class GymSessionReaderPort(Protocol):
    """Reads an athlete's saved gym sessions from the gym's own tables (ADR-0032). Every method
    is scoped to one `user_id` from the authenticated session; another user's session is never
    returned."""

    async def list_sessions(self, user_id: UUID) -> Sequence[StoredGymSession]:
        """Every gym session of the user, in no set order."""
        ...

    async def get_session(self, user_id: UUID, session_id: UUID) -> StoredGymSession | None:
        """One gym session of the user, `None` for an unknown id or another user's."""
        ...
