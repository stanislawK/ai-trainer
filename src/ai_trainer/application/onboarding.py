import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta, timezone
from enum import StrEnum
from uuid import UUID

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.onboarding import OnboardingRepositoryPort
from ai_trainer.domain.goals import Goal
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


MAX_GOAL_LENGTH = 200
# The last place on Earth to reach each calendar date. The athlete's timezone is only
# confirmed on the step after goals, so a target date is "in the past" only once it is
# past everywhere (ADR-0014).
_LAST_TIMEZONE = timezone(timedelta(hours=-12))
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class GoalProblem(StrEnum):
    BLANK = "blank"
    TOO_LONG = "too_long"
    BAD_DATE = "bad_date"
    PAST_DATE = "past_date"


class NoGoalsError(Exception):
    """Raised when the goals step is submitted without a single goal."""


class UnknownGoalSportError(Exception):
    """Raised when a goal names a sport that is not one of the user's own sports."""

    def __init__(self, sport_id: str) -> None:
        super().__init__(f"goal for sport {sport_id!r}, which the user did not pick")
        self.sport_id = sport_id


class InvalidGoalsError(Exception):
    """Raised when any goal row is invalid. `problems` maps each offending row's position
    to what is wrong with it, so the form can flag every row at once."""

    def __init__(self, problems: Mapping[int, GoalProblem]) -> None:
        super().__init__(f"invalid goals at rows {sorted(problems)}")
        self.problems = dict(problems)


def earliest_today(clock: ClockPort) -> date:
    """The earliest calendar date that is still "today" somewhere on Earth."""
    return clock.now().astimezone(_LAST_TIMEZONE).date()


def _parse_goal(sport_id: str | None, text: str, raw_date: str, today: date) -> Goal | GoalProblem:
    if not text:
        return GoalProblem.BLANK
    if len(text) > MAX_GOAL_LENGTH:
        return GoalProblem.TOO_LONG
    raw_date = raw_date.strip()
    if not raw_date:
        return Goal(text=text, target_date=None, sport_id=sport_id)
    # `date.fromisoformat` alone also takes "20270101" and ISO week dates.
    if not _ISO_DATE.fullmatch(raw_date):
        return GoalProblem.BAD_DATE
    try:
        target_date = date.fromisoformat(raw_date)
    except ValueError:
        return GoalProblem.BAD_DATE
    if target_date < today:
        return GoalProblem.PAST_DATE
    return Goal(text=text, target_date=target_date, sport_id=sport_id)


async def set_goals(
    user_id: UUID,
    rows: Sequence[tuple[str | None, str, str]],
    *,
    clock: ClockPort,
    repository: OnboardingRepositoryPort,
) -> None:
    """Stores the athlete's goals (PRD-0003 B9, ADR-0006). Each row is the goal's sport (`None`
    for a general goal), the text typed and the target date as `YYYY-MM-DD`, blank for none.
    A sport must be one of the user's own `user_sports`. Text is trimmed to 1-200 characters,
    and a date may not be before `earliest_today`. A row with neither text nor date is an empty
    card slot and is skipped. Nothing is written unless every row is valid and at least one
    goal remains; problems are keyed by the row's position among `rows`."""
    picked = set(await repository.list_sports(user_id))
    for sport_id, _, _ in rows:
        if sport_id is not None and sport_id not in picked:
            raise UnknownGoalSportError(sport_id)
    today = earliest_today(clock)
    goals: list[Goal] = []
    problems: dict[int, GoalProblem] = {}
    for position, (sport_id, text, raw_date) in enumerate(rows):
        # Postgres text cannot hold NUL; only a hand-crafted request sends one.
        text = text.replace("\x00", "").strip()
        if not text and not raw_date.strip():
            continue
        parsed = _parse_goal(sport_id, text, raw_date, today)
        if isinstance(parsed, GoalProblem):
            problems[position] = parsed
        else:
            goals.append(parsed)
    if problems:
        raise InvalidGoalsError(problems)
    if not goals:
        raise NoGoalsError
    await repository.replace_goals(user_id, goals)
