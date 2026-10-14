from typing import Protocol
from uuid import UUID

from ai_trainer.domain.training import SessionSpan


class TrainingSessionRepositoryPort(Protocol):
    """Reads the athlete's saved training sessions (ADR-0022); only a confirm or an undo of a
    draft writes them, through `SessionDraftRepositoryPort`. Every method is scoped to one
    `user_id` from the authenticated session."""

    async def latest_in_sport(self, user_id: UUID, sport_id: str) -> SessionSpan | None:
        """The user's session in `sport_id` whose latest log was sent last, or `None` when they
        have none (B16, ADR-0014)."""
        ...
