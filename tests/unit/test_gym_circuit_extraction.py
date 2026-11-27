"""`gym.extract` reads rounds and circuits: the exercises of a circuit share its number and
rounds, and code, not the model, gives each one a set per round (PRD 0013 B43, ADR-0032 #618 and
#632 amendments, ticket #633)."""

from typing import Any

import pytest
from pydantic import ValidationError

from ai_trainer.domain.sports.gym.extraction import GymExtraction
from ai_trainer.domain.sports.gym.payload import Exercise


def _extraction(*activities: dict[str, Any]) -> GymExtraction:
    return GymExtraction.model_validate(
        {
            "occurred_on": "2026-11-27",
            "date_source": "assumed_today",
            "activities": list(activities),
        }
    )


def _exercises(*activities: dict[str, Any]) -> list[Exercise]:
    return _extraction(*activities).to_payload().activities


def test_three_rounds_give_one_circuit_of_both_exercises() -> None:
    """The log "3 rounds: 10 burpees, 20 squats" is one circuit of 3 rounds holding both."""
    burpees, squats = _exercises(
        {"name": "burpees", "reps": 10, "circuit": 1, "rounds": 3},
        {"name": "squats", "reps": 20, "circuit": 1, "rounds": 3},
    )

    assert [(e.circuit, e.rounds) for e in (burpees, squats)] == [(1, 3), (1, 3)]
    assert [s.reps for s in burpees.sets] == [10, 10, 10]
    assert [s.reps for s in squats.sets] == [20, 20, 20]


def test_emom_gives_ten_rounds_of_five_pull_ups() -> None:
    """The log "EMOM 10 min: 5 pull-ups" is 10 rounds of 5; the minute is no set's time."""
    (pull_ups,) = _exercises({"name": "pull-ups", "reps": 5, "circuit": 1, "rounds": 10})

    assert (pull_ups.circuit, pull_ups.rounds, pull_ups.measure) == (1, 10, "reps")
    assert [(s.reps, s.duration_s) for s in pull_ups.sets] == [(5, None)] * 10


def test_circuit_keeps_hold_and_per_side() -> None:
    """In "circuit x3: plank 30s, 15 lunges each side" the hold and the per side stay."""
    plank, lunges = _exercises(
        {"name": "plank", "duration": {"value": 30, "unit": "s"}, "circuit": 1, "rounds": 3},
        {"name": "lunges", "reps": 15, "per_side": True, "circuit": 1, "rounds": 3},
    )

    assert (plank.measure, [s.duration_s for s in plank.sets]) == ("time", [30, 30, 30])
    assert (lunges.per_side, [s.reps for s in lunges.sets]) == (True, [15, 15, 15])
    assert {(e.circuit, e.rounds) for e in (plank, lunges)} == {(1, 3)}


def test_plain_sets_are_no_circuit() -> None:
    """The log "3x10 squats, 3x10 rows" is sets, never a circuit."""
    squats, rows = _exercises(
        {"name": "squats", "sets": 3, "reps": 10},
        {"name": "rows", "sets": 3, "reps": 10},
    )

    assert [(e.circuit, e.rounds, len(e.sets)) for e in (squats, rows)] == [
        (None, None, 3),
        (None, None, 3),
    ]


def test_circuit_exercises_never_merge() -> None:
    """Two neighbours alike merge as a pyramid's rows do (#410), but not inside a circuit: two
    rows of the same exercise in one circuit are two stations of each round."""
    first, second = _exercises(
        {"name": "burpees", "reps": 10, "circuit": 1, "rounds": 2},
        {"name": "burpees", "reps": 10, "circuit": 1, "rounds": 2},
    )

    assert [len(e.sets) for e in (first, second)] == [2, 2]


def test_a_circuit_never_merges_with_the_same_exercise_outside_it() -> None:
    plain, in_circuit = _exercises(
        {"name": "burpees", "reps": 10},
        {"name": "burpees", "reps": 10, "circuit": 1, "rounds": 2},
    )

    assert [(e.circuit, len(e.sets)) for e in (plain, in_circuit)] == [(None, 1), (1, 2)]


@pytest.mark.parametrize("given", [{"circuit": 1}, {"rounds": 3}])
def test_circuit_without_rounds_is_sent_back(given: dict[str, int]) -> None:
    with pytest.raises(ValidationError, match="both"):
        _extraction({"name": "burpees", "reps": 10, **given})


def test_circuit_with_sets_is_sent_back() -> None:
    """A circuit's rounds are its set count, so a set count beside them is sent back."""
    with pytest.raises(ValidationError, match="sets"):
        _extraction({"name": "burpees", "sets": 3, "reps": 10, "circuit": 1, "rounds": 3})


def test_split_circuit_is_sent_back() -> None:
    with pytest.raises(ValidationError, match="together"):
        _extraction(
            {"name": "burpees", "reps": 10, "circuit": 1, "rounds": 3},
            {"name": "rows", "sets": 3, "reps": 10},
            {"name": "squats", "reps": 20, "circuit": 1, "rounds": 3},
        )


def test_circuit_differing_in_rounds_is_sent_back() -> None:
    with pytest.raises(ValidationError, match="rounds"):
        _extraction(
            {"name": "burpees", "reps": 10, "circuit": 1, "rounds": 3},
            {"name": "squats", "reps": 20, "circuit": 1, "rounds": 4},
        )


@pytest.mark.parametrize("given", [{"circuit": 0}, {"circuit": 101}, {"rounds": 0}])
def test_circuit_and_rounds_out_of_range_are_sent_back(given: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        _extraction({"name": "burpees", "reps": 10, "circuit": 1, "rounds": 3} | given)


def test_circuits_renumbered_from_one() -> None:
    """Each log numbers its circuits from 1 (ADR-0032 #632), whatever numbers the model used."""
    exercises = _exercises(
        {"name": "burpees", "reps": 10, "circuit": 2, "rounds": 3},
        {"name": "squats", "reps": 20, "circuit": 2, "rounds": 3},
        {"name": "rows", "sets": 3, "reps": 10},
        {"name": "pull-ups", "reps": 5, "circuit": 5, "rounds": 10},
    )

    assert [e.circuit for e in exercises] == [1, 1, None, 2]


def test_v7_output_still_reads_with_no_circuit() -> None:
    (squat,) = _exercises({"name": "squat", "sets": 3, "reps": 5})

    assert (squat.circuit, squat.rounds, len(squat.sets)) == (None, None, 3)
