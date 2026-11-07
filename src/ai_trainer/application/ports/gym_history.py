from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from ai_trainer.domain.history import StoredGymSession


class GymSessionReaderPort(Protocol):
    """Reads an athlete's saved gym sessions from the gym's own tables (ADR-0032). Every method
    is scoped to one `user_id` from the authenticated session; another user's session is never
    returned."""

    async def list_sessions(self, user_id: UUID) -> Sequence[StoredGymSession]:
        """Every gym session of the user, in no set order."""
        ...

    async def get_session(self, user_id: UUID, session_id: UUID) -> StoredGymSession | None:
        """One gym session of the user, or `None` for an unknown one or another user's."""
        ...


class SessionEditorPort(Protocol):
    """Writes the athlete's edit of a saved session (B4, ADR-0022 invariant 3, ticket #516):
    only the athlete's own edit calls it, with `user_id` from the authenticated session."""

    async def replace_activities(
        self,
        user_id: UUID,
        session_id: UUID,
        *,
        sport_id: str,
        payload: Mapping[str, JsonValue],
    ) -> None:
        """In one transaction, replaces the session's activities with `payload` through its
        sport's activity store and bumps `updated_at`. Raises `SessionNotFoundError` for an
        unknown session, another user's, or one of another sport, and `SessionChangedError`
        when the session no longer holds the activities the edit was made on."""
        ...
