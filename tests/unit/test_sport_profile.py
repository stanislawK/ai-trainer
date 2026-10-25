"""The sport part of the onboarding profile: goals, level and schedule, read and typed, converted
and bounded in code (PRD 0008 B33, B34, B35, ADR-0028 invariant 6, ticket #255)."""

from datetime import date

import pytest

from ai_trainer.domain.athlete_profile import FieldProblem
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.sport_profile import (
    LengthUnit,
    SportProfile,
    SportSection,
    Weekday,
    bounded_sessions,
    parse_sport_section,
    session_minutes,
    sport_from_read,
    target_date,
)


def test_hours_become_minutes() -> None:
    assert session_minutes(2, LengthUnit.H) == 120


def test_a_decimal_hour_becomes_whole_minutes() -> None:
    assert session_minutes(1.5, LengthUnit.H) == 90


def test_minutes_stay_minutes() -> None:
    assert session_minutes(90, LengthUnit.MIN) == 90


@pytest.mark.parametrize(("value", "unit"), [(4, LengthUnit.MIN), (11, LengthUnit.H)])
def test_a_session_length_outside_the_bounds_stays_empty(value: float, unit: LengthUnit) -> None:
    assert session_minutes(value, unit) is None


@pytest.mark.parametrize(("sessions", "kept"), [(1, 1), (14, 14), (0, None), (20, None)])
def test_sessions_a_week_are_kept_from_1_to_14(sessions: int, kept: int | None) -> None:
    assert bounded_sessions(sessions) == kept


# An October day in the athlete's own zone (ADR-0014).
TODAY = date(2026, 10, 25)


def test_a_month_and_year_is_the_months_last_day() -> None:
    assert target_date(2027, 6, None, today=TODAY) == date(2027, 6, 30)


def test_a_month_already_past_this_year_is_next_years() -> None:
    """AC: "by June", sent in October, is June of the next year. Code picks the year, not the
    model (ADR-0028 invariant 6)."""
    assert target_date(None, 6, None, today=TODAY) == date(2027, 6, 30)


def test_a_month_still_ahead_is_this_years() -> None:
    assert target_date(None, 12, None, today=TODAY) == date(2026, 12, 31)


def test_the_current_month_is_this_years() -> None:
    assert target_date(None, 10, None, today=TODAY) == date(2026, 10, 31)


def test_a_day_and_month_already_past_this_year_is_next_years() -> None:
    assert target_date(None, 10, 3, today=TODAY) == date(2027, 10, 3)


def test_a_day_and_month_still_ahead_this_year_is_this_years() -> None:
    assert target_date(None, 10, 25, today=TODAY) == date(2026, 10, 25)


def test_a_year_alone_is_its_last_day() -> None:
    assert target_date(2027, None, None, today=TODAY) == date(2027, 12, 31)


def test_a_full_date_stays_as_given() -> None:
    assert target_date(2027, 2, 14, today=TODAY) == date(2027, 2, 14)


def test_february_ends_on_its_own_last_day() -> None:
    assert target_date(2028, 2, None, today=TODAY) == date(2028, 2, 29)


@pytest.mark.parametrize(
    ("year", "month", "day"),
    [
        (2027, 2, 30),
        (2027, 13, None),
        (1800, 6, None),
        (2027, None, 5),
        (None, None, None),
        (None, 2, 30),
    ],
)
def test_an_impossible_date_stays_empty(
    year: int | None, month: int | None, day: int | None
) -> None:
    assert target_date(year, month, day, today=TODAY) is None


def test_weekdays_count_from_monday() -> None:
    assert [day.number for day in (Weekday.MON, Weekday.SUN)] == [0, 6]


def test_a_read_fills_the_sport_section() -> None:
    section = sport_from_read(
        "climbing",
        goals={"goals": [{"text": "Send my first 7c outdoors", "target_date": "2027-06-30"}]},
        level={"level": "Projecting 7b", "exercises": "Board, hangboard"},
        schedule={"sessions_per_week": 4, "session_minutes": 120, "preferred_days": [0, 2, 4, 5]},
    )

    assert section == SportSection(
        goals=(
            Goal(
                text="Send my first 7c outdoors", target_date=date(2027, 6, 30), sport_id="climbing"
            ),
        ),
        profile=SportProfile(
            level="Projecting 7b",
            exercises="Board, hangboard",
            sessions_per_week=4,
            session_minutes=120,
            preferred_days=(0, 2, 4, 5),
        ),
    )


def test_no_reads_leave_the_section_empty() -> None:
    assert sport_from_read("gym", goals=None, level=None, schedule=None) == SportSection(
        goals=(), profile=SportProfile()
    )


def test_a_read_value_of_the_wrong_shape_or_out_of_bounds_stays_empty() -> None:
    section = sport_from_read(
        "gym",
        goals={"goals": [{"text": "", "target_date": "June"}, {"text": 5}, "x", {"text": "Ok"}]},
        level={"level": 7, "exercises": "x" * 400},
        schedule={"sessions_per_week": 20, "session_minutes": True, "preferred_days": [9, "1", 3]},
    )

    assert section.goals == (Goal(text="Ok", sport_id="gym"),)
    assert section.profile == SportProfile(exercises="x" * 300, preferred_days=(3,))


def test_a_read_goal_with_a_bad_date_keeps_its_text() -> None:
    section = sport_from_read(
        "gym",
        goals={"goals": [{"text": "Deadlift 150 kg", "target_date": "soon"}]},
        level=None,
        schedule=None,
    )

    assert section.goals == (Goal(text="Deadlift 150 kg", sport_id="gym"),)


def test_at_most_five_goals_are_read() -> None:
    goals = [{"text": f"Goal {n}"} for n in range(7)]

    section = sport_from_read("gym", goals={"goals": goals}, level=None, schedule=None)

    assert [goal.text for goal in section.goals] == [f"Goal {n}" for n in range(5)]


def _typed(**fields: str) -> dict[str, str]:
    return {
        "level": "",
        "exercises": "",
        "sessions_per_week": "",
        "session_minutes": "",
        **fields,
    }


def test_the_typed_section_is_parsed() -> None:
    section = parse_sport_section(
        "climbing",
        _typed(level="7b", exercises="Board", sessions_per_week="4", session_minutes="120"),
        days=["5", "0", "2", "0"],
        goal_texts=["First 7c outdoors", ""],
        goal_dates=["2027-06-30", ""],
    )

    assert section == SportSection(
        goals=(Goal(text="First 7c outdoors", target_date=date(2027, 6, 30), sport_id="climbing"),),
        profile=SportProfile(
            level="7b",
            exercises="Board",
            sessions_per_week=4,
            session_minutes=120,
            preferred_days=(0, 2, 5),
        ),
    )


def test_a_blank_section_is_empty() -> None:
    section = parse_sport_section("gym", _typed(), days=[], goal_texts=[""], goal_dates=[""])

    assert section == SportSection(goals=(), profile=SportProfile())


def test_twenty_sessions_a_week_are_rejected() -> None:
    problems = parse_sport_section(
        "gym", _typed(sessions_per_week="20"), days=[], goal_texts=[], goal_dates=[]
    )

    assert problems == {"sessions_per_week": FieldProblem.OUT_OF_RANGE}


def test_every_bad_field_is_named_at_once() -> None:
    problems = parse_sport_section(
        "gym",
        _typed(level="x" * 201, exercises="x" * 301, sessions_per_week="0", session_minutes="1.5"),
        days=["7"],
        goal_texts=["x" * 201, "", "Squat"],
        goal_dates=["", "2027-06-30", "30.06.2027"],
    )

    assert problems == {
        "level": FieldProblem.OUT_OF_RANGE,
        "exercises": FieldProblem.OUT_OF_RANGE,
        "sessions_per_week": FieldProblem.OUT_OF_RANGE,
        "session_minutes": FieldProblem.OUT_OF_RANGE,
        "days": FieldProblem.OUT_OF_RANGE,
        "goal_text.0": FieldProblem.OUT_OF_RANGE,
        "goal_text.1": FieldProblem.REQUIRED,
        "goal_date.2": FieldProblem.OUT_OF_RANGE,
    }


def test_an_impossible_typed_date_is_rejected() -> None:
    problems = parse_sport_section(
        "gym", _typed(), days=[], goal_texts=["Squat"], goal_dates=["2027-02-30"]
    )

    assert problems == {"goal_date.0": FieldProblem.OUT_OF_RANGE}


def test_a_goal_without_a_date_has_none() -> None:
    section = parse_sport_section(
        "gym", _typed(), days=[], goal_texts=[" Squat 120 kg "], goal_dates=[]
    )

    assert isinstance(section, SportSection)
    assert section.goals == (Goal(text="Squat 120 kg", sport_id="gym"),)
