"""What one entry plots on an exercise's progress chart (PRD 0011 B38, B39, ticket #517)."""

from datetime import date
from uuid import uuid4

import pytest

from ai_trainer.domain.exercise_progress import EntryBest, best_of
from ai_trainer.domain.exercises import ExerciseEntry, ExerciseMeasure, LoggedSet


def _entry(measure: ExerciseMeasure, *sets: LoggedSet) -> ExerciseEntry:
    return ExerciseEntry(
        session_id=uuid4(),
        occurred_on=date(2026, 11, 1),
        name_as_typed="x",
        measure=measure,
        per_side=False,
        sets=sets,
    )


def test_load_reps_plots_heaviest_set() -> None:
    entry = _entry(
        ExerciseMeasure.LOAD_REPS,
        LoggedSet(set_no=1, reps=12, load_kg=6.0),
        LoggedSet(set_no=2, reps=8, load_kg=7.5),
        LoggedSet(set_no=3, reps=6, load_kg=7.5),
        LoggedSet(set_no=4, reps=10),
    )

    assert best_of(entry, []) == EntryBest(value=7.5, text="8 @ 7.5 kg")


def test_load_reps_without_reps_shows_only_the_load() -> None:
    entry = _entry(ExerciseMeasure.LOAD_REPS, LoggedSet(set_no=1, load_kg=6.0))

    assert best_of(entry, []) == EntryBest(value=6.0, text="6 kg")


def test_reps_time_and_distance_bests() -> None:
    reps = _entry(ExerciseMeasure.REPS, LoggedSet(set_no=1, reps=1), LoggedSet(set_no=2, reps=12))
    one = _entry(ExerciseMeasure.REPS, LoggedSet(set_no=1, reps=1))
    time = _entry(
        ExerciseMeasure.TIME, LoggedSet(set_no=1, duration_s=30), LoggedSet(set_no=2, duration_s=45)
    )
    distance = _entry(ExerciseMeasure.DISTANCE, LoggedSet(set_no=1, distance_m=40.0))

    assert best_of(reps, []) == EntryBest(value=12.0, text="12 reps")
    assert best_of(one, []) == EntryBest(value=1.0, text="1 rep")
    assert best_of(time, []) == EntryBest(value=45.0, text="45 s")
    assert best_of(distance, []) == EntryBest(value=40.0, text="40 m")


def test_band_plots_place_in_the_athletes_order() -> None:
    entry = _entry(
        ExerciseMeasure.BAND_REPS,
        LoggedSet(set_no=1, reps=10, band_label="red"),
        LoggedSet(set_no=2, reps=8, band_label="black"),
    )

    assert best_of(entry, ["black", "green", "red"]) == EntryBest(value=3.0, text="red band")


def test_unranked_band_has_no_value_and_says_so() -> None:
    entry = _entry(ExerciseMeasure.BAND_REPS, LoggedSet(set_no=1, reps=10, band_label="blue"))

    assert best_of(entry, ["red"]) == EntryBest(value=None, text="blue · not in your order")


def test_set_missing_its_value_is_skipped_never_estimated() -> None:
    for measure in ExerciseMeasure:
        assert best_of(_entry(measure, LoggedSet(set_no=1)), []) is None


def test_load_reps_without_kg_falls_back_to_most_reps_and_plots_nothing() -> None:
    entry = _entry(
        ExerciseMeasure.LOAD_REPS,
        LoggedSet(set_no=1, reps=8),
        LoggedSet(set_no=2, reps=10),
    )

    assert best_of(entry, []) == EntryBest(value=None, text="10 reps")


def test_fallback_reads_any_other_value_a_set_carries() -> None:
    assert best_of(
        _entry(ExerciseMeasure.REPS, LoggedSet(set_no=1, load_kg=20.0)), []
    ) == EntryBest(value=None, text="20 kg")
    assert best_of(
        _entry(ExerciseMeasure.LOAD_REPS, LoggedSet(set_no=1, duration_s=30)), []
    ) == EntryBest(value=None, text="30 s")
    assert best_of(
        _entry(ExerciseMeasure.TIME, LoggedSet(set_no=1, distance_m=40.0)), []
    ) == EntryBest(value=None, text="40 m")
    assert best_of(
        _entry(ExerciseMeasure.REPS, LoggedSet(set_no=1, band_label="red")), []
    ) == EntryBest(value=None, text="red band")


def test_distance_time_plots_speed_of_the_fastest_set_and_says_its_pace() -> None:
    """Faster is higher: the value is km/h, the text the pace per split (B42, #626)."""
    entry = _entry(
        ExerciseMeasure.DISTANCE_TIME,
        LoggedSet(set_no=1, distance_m=2000.0, duration_s=480),
        LoggedSet(set_no=2, distance_m=2000.0, duration_s=470),
        LoggedSet(set_no=3, duration_s=300),
    )

    per_500m = best_of(entry, [], split="500m")
    per_km = best_of(entry, [])

    assert per_500m is not None
    assert per_500m.value == pytest.approx(15.319, abs=1e-3)
    assert per_500m.text == "1:57.5"
    assert per_km is not None
    assert per_km.text == "3:55"


def test_distance_time_without_a_pace_is_not_plotted() -> None:
    """A time with no distance has no pace: its text is the time, and nothing is plotted."""
    entry = _entry(ExerciseMeasure.DISTANCE_TIME, LoggedSet(set_no=1, duration_s=470))

    assert best_of(entry, []) == EntryBest(value=None, text="470 s")


def test_an_entry_is_read_by_the_charts_measure_when_given() -> None:
    """A rower logged with only a time is a `time` entry, and on a `distance_time` chart it is
    read by that chart's measure: no pace, so no point."""
    timed = _entry(ExerciseMeasure.TIME, LoggedSet(set_no=1, duration_s=470))

    assert best_of(timed, []) == EntryBest(value=470.0, text="470 s")
    assert best_of(timed, [], measure=ExerciseMeasure.DISTANCE_TIME) == EntryBest(
        value=None, text="470 s"
    )
