from collections.abc import Mapping
from uuid import UUID
from zoneinfo import available_timezones

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.onboarding import OnboardingRepositoryPort

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


class InvalidTimezoneError(Exception):
    """Raised when the timezone step is submitted with something that is not an IANA zone."""


def timezone_names() -> tuple[str, ...]:
    """The IANA zones an athlete can pick, sorted: `Region/City` names plus `UTC`. The
    bare legacy aliases `zoneinfo` also knows (`Poland`, `Factory`, `EST`) are left out."""
    return tuple(sorted(name for name in available_timezones() if "/" in name or name == "UTC"))


async def confirm_timezone(
    user_id: UUID,
    raw_zone: str,
    *,
    clock: ClockPort,
    repository: OnboardingRepositoryPort,
) -> None:
    """Stores the athlete's IANA timezone and finishes onboarding (PRD-0003 F6, G9,
    ADR-0014). `onboarded_at` comes from the `Clock` port. The name must be one of
    `timezone_names()`, exactly as spelled; anything else raises `InvalidTimezoneError` and
    writes nothing."""
    zone = raw_zone.strip()
    if zone not in timezone_names():
        raise InvalidTimezoneError
    await repository.finish_onboarding(user_id, zone, clock.now())
