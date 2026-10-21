from collections.abc import Mapping, Sequence
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from ai_trainer.domain.training import SessionSpan


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
