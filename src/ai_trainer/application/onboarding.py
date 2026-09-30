from collections.abc import Sequence
from uuid import UUID

from ai_trainer.application.ports.onboarding import OnboardingRepositoryPort
from ai_trainer.domain.sports.registry import SportRegistry


class EmptySportSelectionError(Exception):
    """Raised when the sports step is submitted with nothing picked."""


async def choose_sports(
    user_id: UUID,
    sport_ids: Sequence[str],
    *,
    registry: SportRegistry,
    repository: OnboardingRepositoryPort,
) -> None:
    """Stores exactly the picked sports (ADR-0006). Every ID is checked against
    `SportRegistry` before anything is written, so a bad request stores nothing; an unknown
    ID raises `UnknownSportError`."""
    picked = list(dict.fromkeys(sport_ids))
    if not picked:
        raise EmptySportSelectionError
    for sport_id in picked:
        registry.get(sport_id)
    await repository.replace_sports(user_id, picked)
