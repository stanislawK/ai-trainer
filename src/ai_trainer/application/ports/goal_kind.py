from typing import Protocol
from uuid import UUID

from ai_trainer.domain.goals import GoalKind


class GoalKindPort(Protocol):
    """Which kind an onboarding goal is (PRD 0013 B45, ADR-0028 #618 amendment), picked in the
    goals step's background read and shown on the summary for the athlete to confirm."""

    async def kind_of(self, user_id: UUID, goal: str) -> GoalKind | None:
        """The goal's kind when the pick is confident. `None` when it can't be placed, the pick
        is unsure, or the call failed. Never raises."""
        ...
