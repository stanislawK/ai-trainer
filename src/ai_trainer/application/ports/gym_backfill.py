from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue

from ai_trainer.domain.sports.gym.rows import GymEntryRow


class BackfillCandidate(BaseModel):
    """A gym session whose activities are still only in its v1 payload."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    session_id: UUID
    payload: Mapping[str, JsonValue]
    payload_version: int


class GymBackfillPort(Protocol):
    """Reads the gym sessions that have no backfilled rows and writes them (ADR-0032, #377).
    It only inserts: a session's stored payload is never changed (ADR-0022 invariant 4)."""

    async def candidates(self) -> list[BackfillCandidate]:
        """Gym sessions with a non-empty payload and no entry without a source, so a second
        run finds none."""
        ...

    async def write(self, candidate: BackfillCandidate, entries: Sequence[GymEntryRow]) -> None:
        """Inserts the entries and their sets for the candidate's own user in one transaction,
        with `exercise_id` and `source_id` NULL, after the session's existing entries."""
        ...
