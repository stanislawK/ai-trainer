from collections.abc import Mapping, Sequence
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


WEEKDAYS = range(7)
MAX_MINUTES_PER_DAY = 600


class NoAvailabilityError(Exception):
    """Raised when every day is 0 minutes: the athlete must be able to train at least once."""


class InvalidAvailabilityError(Exception):
    """Raised when a day's minutes are not a whole number from 0 to 600, or the weekday is
    unknown. `weekdays` names every offending day so the form can flag each one."""

    def __init__(self, weekdays: frozenset[int]) -> None:
        super().__init__(f"invalid availability for weekdays {sorted(weekdays)}")
        self.weekdays = weekdays


def _parse_minutes(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return 0
    # ASCII digits only: `int()` alone also takes "+5", "1_0" and non-ASCII digits.
    if not (text.isascii() and text.isdigit()) or len(text) > len(str(MAX_MINUTES_PER_DAY)):
        return None
    minutes = int(text)
    return minutes if 0 <= minutes <= MAX_MINUTES_PER_DAY else None


async def set_availability(
    user_id: UUID,
    raw_minutes: Mapping[int, str],
    *,
    repository: OnboardingRepositoryPort,
) -> None:
    """Stores the days the athlete can train (PRD-0003 B10, ADR-0006). `raw_minutes` maps a
    weekday (0 = Monday) to the text typed; blank means 0. Only days above 0 are stored, and
    nothing is written unless every day is valid and at least one is above 0."""
    parsed: dict[int, int] = {}
    invalid: set[int] = set()
    for weekday, raw in raw_minutes.items():
        minutes = _parse_minutes(raw) if weekday in WEEKDAYS else None
        if minutes is None:
            invalid.add(weekday)
        else:
            parsed[weekday] = minutes
    if invalid:
        raise InvalidAvailabilityError(frozenset(invalid))
    trainable = {weekday: minutes for weekday, minutes in parsed.items() if minutes > 0}
    if not trainable:
        raise NoAvailabilityError
    await repository.replace_availability(user_id, trainable)
