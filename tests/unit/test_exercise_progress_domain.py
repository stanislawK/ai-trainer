"""What one entry plots on an exercise's progress chart (PRD 0011 B38, B39, ticket #517)."""

from datetime import date
from uuid import uuid4

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
