"""`gym.extract` reads a machine's incline, level and watts only as the log gives them, onto
every set of that exercise; speed and other settings with no field stay out (PRD 0013 B38, B44,
ADR-0032 #618 amendment, ticket #631)."""

from typing import Any

import pytest
from pydantic import ValidationError

from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.sports.gym.payload import Exercise


def _extraction(**activity: Any) -> GymExtraction:
    return GymExtraction.model_validate(
        {"occurred_on": "2026-11-25", "date_source": "assumed_today", "activities": [activity]}
    )


def _only_exercise(**activity: Any) -> Exercise:
    (exercise,) = _extraction(**activity).to_payload().activities
    return exercise


def _settings(exercise: Exercise) -> list[tuple[float | None, int | None, int | None]]:
    return [(s.incline_pct, s.machine_level, s.watts) for s in exercise.sets]


def test_treadmill_incline_goes_on_every_set_without_level_or_watts() -> None:
    """For "20 min treadmill incline 5 % speed 6" speed has no field, so only incline is read."""
    exercise = _only_exercise(
        name="treadmill", sets=2, duration={"value": 20, "unit": "min"}, incline=5
    )

    assert _settings(exercise) == [(5.0, None, None), (5.0, None, None)]
    assert [s.duration_s for s in exercise.sets] == [1200, 1200]


def test_bike_erg_watts_with_time_in_seconds() -> None:
    exercise = _only_exercise(name="bike erg", duration={"value": 10, "unit": "min"}, watts=200)

    (gym_set,) = exercise.sets
    assert (gym_set.watts, gym_set.duration_s) == (200, 600)
    assert (gym_set.incline_pct, gym_set.machine_level) == (None, None)


def test_cross_trainer_level() -> None:
    exercise = _only_exercise(name="cross-trainer", duration={"value": 15, "unit": "min"}, level=8)

    assert _settings(exercise) == [(None, 8, None)]


def test_no_settings_given_leaves_all_three_empty() -> None:
    exercise = _only_exercise(name="treadmill", duration={"value": 20, "unit": "min"})

    assert _settings(exercise) == [(None, None, None)]


def test_a_decline_reads_below_zero() -> None:
    assert _settings(_only_exercise(name="treadmill", incline=-2.5)) == [(-2.5, None, None)]


@pytest.mark.parametrize(
    "setting",
    [
        {"incline": 45},
        {"incline": -11},
        {"incline": float("nan")},
        {"level": 0},
        {"level": 51},
        {"watts": 0},
        {"watts": 2001},
    ],
)
def test_settings_out_of_range_are_rejected(setting: dict[str, Any]) -> None:
    """The model's own output fails, so it is sent back to retry."""
    with pytest.raises(ValidationError):
        _extraction(name="treadmill", **setting)


def test_settings_alone_keep_the_measure_from_time() -> None:
    exercise = _only_exercise(
        name="treadmill", duration={"value": 20, "unit": "min"}, incline=5, level=3, watts=150
    )

    assert exercise.measure == "time"


def test_v6_output_still_reads() -> None:
    """An output with none of the new keys, as v6 gave it, validates with all three empty."""
    exercise = _only_exercise(name="squat", sets=3, reps=5)

    assert exercise.measure == "reps"
    assert _settings(exercise) == [(None, None, None)] * 3
