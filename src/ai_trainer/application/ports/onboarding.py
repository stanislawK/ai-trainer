from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.goals import Goal


class OnboardingRepositoryPort(Protocol):
    """Persists what onboarding collects about the athlete (ADR-0006). Every method is
    scoped to one `user_id`, which comes from the authenticated session (ADR-0005)."""

    async def replace_sports(
        self,
        user_id: UUID,
        sport_ids: Sequence[str],
        *,
        general_goals_of: Sequence[str] = (),
        delete_goals_of: Sequence[str] = (),
    ) -> None:
        """Makes `sport_ids` exactly the user's `user_sports` rows. In the same transaction,
        the user's goals of the sports in `general_goals_of` lose their sport and the goals
        of those in `delete_goals_of` are deleted; every other goal is untouched."""
        ...

    async def list_sports(self, user_id: UUID) -> Sequence[str]: ...

    async def replace_availability(
        self, user_id: UUID, minutes_by_weekday: Mapping[int, int]
    ) -> None:
        """Makes `minutes_by_weekday` (weekday 0 = Monday to minutes) exactly the user's
        `weekly_availability` rows."""
        ...

    async def list_availability(self, user_id: UUID) -> Mapping[int, int]: ...

    async def replace_goals(self, user_id: UUID, goals: Sequence[Goal]) -> None:
        """Makes `goals` exactly the user's `goals` rows, keeping their order."""
        ...

    async def list_goals(self, user_id: UUID) -> Sequence[Goal]: ...

    async def set_onboarded_at(self, user_id: UUID, when: datetime | None) -> None: ...

    async def finish_onboarding(self, user_id: UUID, timezone: str, when: datetime) -> None:
        """Stores the user's IANA `timezone` and sets `onboarded_at` to `when`, together."""
        ...
