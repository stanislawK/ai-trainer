from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue


class LoggedActivities(BaseModel):
    """One log's activities in its session: the confirmed draft's payload (`addition`), the
    source row that log wrote, and the session's stored payload, `None` for a session the log
    creates."""

    model_config = ConfigDict(frozen=True)

    session_id: UUID
    source_id: UUID
    session_payload: Mapping[str, JsonValue] | None
    addition: Mapping[str, JsonValue]


class ActivityStorePort[Tx](Protocol):
    """Where a sport keeps a confirmed session's activities (ADR-0032): its own tables, or the
    session's payload by default. The confirm and undo transactions call it inside their own
    transaction `Tx`, mapped by `sport_id` in the composition root, so core code never branches
    on a sport. Every method is scoped to one `user_id` from the authenticated session, and
    only a confirm or an undo of the athlete's own draft calls `add` and `withdraw` (ADR-0022
    invariant 3)."""

    async def add(self, tx: Tx, user_id: UUID, log: LoggedActivities) -> dict[str, JsonValue]:
        """Writes the log's activities after the session's own, and returns the payload the
        session keeps."""
        ...

    async def withdraw(self, tx: Tx, user_id: UUID, log: LoggedActivities) -> dict[str, JsonValue]:
        """Takes the log's activities back from a session that keeps other logs (B4, B16), and
        returns the payload the session keeps. Called before the log's source row goes."""
        ...

    async def recent(
        self, tx: Tx, user_id: UUID, session_ids: Sequence[UUID]
    ) -> list[Mapping[str, JsonValue]]:
        """What the sport's `ExtractionSpec.recent` reads, one mapping per session in the order
        of `session_ids`; nothing of another user's session is read."""
        ...
