"""The sport part of the onboarding profile (PRD 0008 B33, ADR-0028): per sport, goals with
optional target dates, the current level and exercises, sessions a week, session length and
preferred days. Every field is optional. Lengths and dates are worked out here, in code, never in
a prompt (invariant 6); weekday 0 is Monday."""

import calendar
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from ai_trainer.domain.athlete_profile import FieldProblem, typed_whole
from ai_trainer.domain.goals import Goal

MAX_GOALS = 5
MAX_GOAL_LENGTH = 200
MAX_LEVEL_LENGTH = 200
MAX_EXERCISES_LENGTH = 300
MIN_SESSIONS, MAX_SESSIONS = 1, 14
MIN_SESSION_MINUTES, MAX_SESSION_MINUTES = 5, 600
MIN_GOAL_YEAR, MAX_GOAL_YEAR = 1900, 2100

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_WEEKDAY = re.compile(r"[0-6]")


class LengthUnit(StrEnum):
    MIN = "min"
    H = "h"


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"

    @property
    def number(self) -> int:
        """0 for Monday to 6 for Sunday, as `date.weekday()` counts."""
        return list(Weekday).index(self)


class SportProfile(BaseModel):
    """One sport's stored profile (`sport_profiles`)."""

    model_config = ConfigDict(frozen=True)

    level: str | None = None
    exercises: str | None = None
    sessions_per_week: int | None = None
    session_minutes: int | None = None
    preferred_days: tuple[int, ...] = ()


class SportSection(BaseModel):
    """What the summary shows and the athlete edits for one sport (B35)."""

    model_config = ConfigDict(frozen=True)

    goals: tuple[Goal, ...] = ()
    profile: SportProfile = SportProfile()


class SportForm(BaseModel):
    """One sport's summary fields as posted: the single fields by name, the checked days, and
    each goal's text and date by position."""

    model_config = ConfigDict(frozen=True)

    fields: dict[str, str] = {}
    days: tuple[str, ...] = ()
    goal_texts: tuple[str, ...] = ()
    goal_dates: tuple[str, ...] = ()


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def session_minutes(value: float, unit: LengthUnit) -> int | None:
    """`value` in `unit` as whole minutes, or `None` outside the bounds."""
    minutes = _round_half_up(value * 60 if unit is LengthUnit.H else value)
    return minutes if MIN_SESSION_MINUTES <= minutes <= MAX_SESSION_MINUTES else None


def bounded_sessions(sessions: int) -> int | None:
    return sessions if MIN_SESSIONS <= sessions <= MAX_SESSIONS else None


def target_date(
    year: int | None, month: int | None, day: int | None, *, today: date
) -> date | None:
    """The date a goal names, on the athlete's own `today` (B15): a month alone ("by June") is
    its last day, and a year alone its last day too. With no year, the date is the next one to
    come: this year's when it is today or later, else next year's. That pick is arithmetic, so
    it is code's (ADR-0028 invariant 6). A day without a month, or a date that doesn't exist,
    is `None`."""
    if month is None and (year is None or day is not None):
        return None
    if year is None:
        upcoming = _on(today.year, month, day)
        if upcoming is not None and upcoming < today:
            return _on(today.year + 1, month, day)
        return upcoming
    if not MIN_GOAL_YEAR <= year <= MAX_GOAL_YEAR:
        return None
    return _on(year, month or 12, day)


def _on(year: int, month: int | None, day: int | None) -> date | None:
    """`day` of `month` in `year`, or the month's last day; `None` when it doesn't exist."""
    if month is None or not 1 <= month <= 12:
        return None
    last = calendar.monthrange(year, month)[1]
    day = day or last
    return date(year, month, day) if 1 <= day <= last else None


def bounded_text(text: str, length: int) -> str | None:
    """`text` trimmed and cut to `length`; `None` when blank."""
    cut = text.replace("\x00", "").strip()[:length].strip()
    return cut or None


def _iso_date(raw: str) -> date | None:
    if not _ISO_DATE.fullmatch(raw):
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if MIN_GOAL_YEAR <= parsed.year <= MAX_GOAL_YEAR else None


def _whole(value: object) -> int | None:
    # `bool` is an `int`; a read never means a number by it.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(parsed: Mapping[str, object], key: str, length: int) -> str | None:
    value = parsed.get(key)
    return bounded_text(value, length) if isinstance(value, str) else None


def _goals_from_read(sport_id: str, parsed: Mapping[str, object]) -> tuple[Goal, ...]:
    read = parsed.get("goals")
    goals: list[Goal] = []
    for item in read if isinstance(read, list) else []:
        if not isinstance(item, dict) or (text := _text(item, "text", MAX_GOAL_LENGTH)) is None:
            continue
        when = item.get("target_date")
        goals.append(
            Goal(
                text=text,
                target_date=_iso_date(when) if isinstance(when, str) else None,
                sport_id=sport_id,
            )
        )
    return tuple(goals[:MAX_GOALS])


def sport_from_read(
    sport_id: str,
    *,
    goals: Mapping[str, object] | None,
    level: Mapping[str, object] | None,
    schedule: Mapping[str, object] | None,
) -> SportSection:
    """What the reads stored, as the summary shows it: a value of the wrong type or outside the
    bounds stays empty, as a value the answer left unclear does (B34)."""
    level, schedule = level or {}, schedule or {}
    sessions, minutes = (
        _whole(schedule.get("sessions_per_week")),
        _whole(schedule.get("session_minutes")),
    )
    days = schedule.get("preferred_days")
    return SportSection(
        goals=_goals_from_read(sport_id, goals or {}),
        profile=SportProfile(
            level=_text(level, "level", MAX_LEVEL_LENGTH),
            exercises=_text(level, "exercises", MAX_EXERCISES_LENGTH),
            sessions_per_week=bounded_sessions(sessions) if sessions is not None else None,
            session_minutes=minutes
            if minutes is not None and MIN_SESSION_MINUTES <= minutes <= MAX_SESSION_MINUTES
            else None,
            preferred_days=tuple(
                sorted(
                    {
                        number
                        for day in (days if isinstance(days, list) else [])
                        if (number := _whole(day)) is not None and 0 <= number <= 6
                    }
                )
            ),
        ),
    )


def _typed_text(raw: str, length: int) -> str | FieldProblem | None:
    text = raw.replace("\x00", "").strip()
    if len(text) > length:
        return FieldProblem.OUT_OF_RANGE
    return text or None


def _typed_goals(
    sport_id: str, texts: Sequence[str], dates: Sequence[str], problems: dict[str, FieldProblem]
) -> tuple[Goal, ...]:
    goals: list[Goal] = []
    count = min(max(len(texts), len(dates)), MAX_GOALS)
    for n in range(count):
        text = _typed_text(texts[n] if n < len(texts) else "", MAX_GOAL_LENGTH)
        raw_date = (dates[n] if n < len(dates) else "").strip()
        when = _iso_date(raw_date) if raw_date else None
        if raw_date and when is None:
            problems[f"goal_date.{n}"] = FieldProblem.OUT_OF_RANGE
        if isinstance(text, FieldProblem):
            problems[f"goal_text.{n}"] = text
        elif text is None and raw_date:
            problems[f"goal_text.{n}"] = FieldProblem.REQUIRED
        elif text is not None:
            goals.append(Goal(text=text, target_date=when, sport_id=sport_id))
    return tuple(goals)


def parse_sport_section(
    sport_id: str,
    form: Mapping[str, str],
    *,
    days: Sequence[str],
    goal_texts: Sequence[str],
    goal_dates: Sequence[str],
) -> SportSection | dict[str, FieldProblem]:
    """One sport's summary fields as typed (B35): blank is empty, and every field outside its
    bounds is named at once. Goals pair their text and date by position; a goal with neither is
    dropped, and a date needs its goal."""
    values = {
        "level": _typed_text(form.get("level", ""), MAX_LEVEL_LENGTH),
        "exercises": _typed_text(form.get("exercises", ""), MAX_EXERCISES_LENGTH),
        "sessions_per_week": typed_whole(
            form.get("sessions_per_week", ""), MIN_SESSIONS, MAX_SESSIONS
        ),
        "session_minutes": typed_whole(
            form.get("session_minutes", ""), MIN_SESSION_MINUTES, MAX_SESSION_MINUTES
        ),
    }
    problems = {field: value for field, value in values.items() if isinstance(value, FieldProblem)}
    if not all(_WEEKDAY.fullmatch(day) for day in days):
        problems["days"] = FieldProblem.OUT_OF_RANGE
    goals = _typed_goals(sport_id, goal_texts, goal_dates, problems)
    if problems:
        return problems
    return SportSection(
        goals=goals,
        profile=SportProfile.model_validate(
            {**values, "preferred_days": tuple(sorted({int(day) for day in days}))}
        ),
    )
