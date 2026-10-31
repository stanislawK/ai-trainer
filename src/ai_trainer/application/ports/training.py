from collections.abc import Mapping, Sequence
from datetime import date
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from ai_trainer.domain.training import SessionSpan, SessionSummary


class SessionHistoryPort(Protocol):
    """Lists the athlete's saved sessions for a history read (ADR-0010, ticket #378), scoped to
    one `user_id` from the authenticated session: another user's session is never listed."""

    async def list_sessions(
        self,
        user_id: UUID,
        *,
        sport_id: str | None,
        since: date | None,
        until: date | None,
        limit: int,
    ) -> Sequence[SessionSummary]:
        """At most `limit` of the user's sessions, the latest day first, in `sport_id` when
        given and on days from `since` to `until`, both included, when given."""
        ...


class TrainingSessionRepositoryPort(Protocol):
    """Reads the athlete's saved training sessions (ADR-0022); only a confirm or an undo of a
    draft writes them, through `SessionDraftRepositoryPort`. Every method is scoped to one
    `user_id` from the authenticated session."""

    async def latest_in_sport(self, user_id: UUID, sport_id: str) -> SessionSpan | None:
        """The user's session in `sport_id` whose latest log was sent last, or `None` when they
        have none (B16, ADR-0014)."""
        ...

    async def recent_payloads(
        self, user_id: UUID, sport_id: str, limit: int
    ) -> Sequence[Mapping[str, JsonValue]]:
        """The stored payloads of the user's `limit` latest sessions in `sport_id`, the latest
        day first, for the sport's plugin to read its context from (ticket #164)."""
        ...
