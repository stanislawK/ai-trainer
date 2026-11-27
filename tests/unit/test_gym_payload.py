"""Gym payload v8: a circuit's exercises, each with one set per round (PRD 0013 B43, ADR-0032
#618 amendment, ticket #632)."""

from typing import Any

import pytest
from pydantic import ValidationError

from ai_trainer.domain.sports.gym.payload import GymPayload


def _exercise(name: str, *, sets: int, **fields: Any) -> dict[str, Any]:
    return {
        "name_as_typed": name,
        "measure": "reps",
        "sets": [{"reps": 10}] * sets,
        **fields,
    }


def test_circuit_exercises_share_rounds_and_set_count() -> None:
    """ "3 rounds of 10 burpees and 15 squats" is two exercises in circuit 1 of 3 rounds, each
    round a set."""
    payload = GymPayload.model_validate(
        {
            "activities": [
                _exercise("burpees", sets=3, circuit=1, rounds=3),
                _exercise("squats", sets=3, circuit=1, rounds=3),
                _exercise("plank", sets=1),
            ]
        }
    )

    burpees, squats, plank = payload.activities
    assert (burpees.circuit, burpees.rounds, len(burpees.sets)) == (1, 3, 3)
    assert (squats.circuit, squats.rounds, len(squats.sets)) == (1, 3, 3)
    assert (plank.circuit, plank.rounds) == (None, None)


def test_a_circuit_of_one_exercise_is_kept() -> None:
    payload = GymPayload.model_validate(
        {"activities": [_exercise("burpees", sets=2, circuit=1, rounds=2)]}
    )

    assert payload.activities[0].rounds == 2


_V6_SQUAT: dict[str, Any] = {
    "sport": "gym",
    "activities": [
        {
            "exercise_id": None,
            "keep_name": False,
            "jev_pick": False,
            "candidate_scores": {},
            "name_as_typed": "squat",
            "measure": "reps",
            "per_side": False,
            "implements": None,
            "rir": None,
            "sets": [
                {
                    "reps": 10,
                    "load": None,
                    "duration_s": None,
                    "distance_m": None,
                    "band_label": None,
                }
            ],
        }
    ],
}
_V7_SQUAT: dict[str, Any] = {
    **_V6_SQUAT,
    "activities": [
        {
            **_V6_SQUAT["activities"][0],
            "sets": [{**_V6_SQUAT["activities"][0]["sets"][0], "watts": None}],
        }
    ],
}


@pytest.mark.parametrize("stored", [_V6_SQUAT, _V7_SQUAT], ids=["v6", "v7"])
def test_v6_and_v7_documents_read_with_no_circuit(stored: dict[str, Any]) -> None:
    """A draft written before circuits reads as it was, with no circuit, and the same again once
    written back (ADR-0022 invariant 4)."""
    payload = GymPayload.model_validate(stored)

    (squat,) = payload.activities
    assert (squat.circuit, squat.rounds) == (None, None)
    assert squat.sets[0].reps == 10
    assert GymPayload.model_validate(payload.model_dump(mode="json")) == payload


@pytest.mark.parametrize(
    "activities",
    [
        [_exercise("burpees", sets=3, circuit=1)],
        [_exercise("burpees", sets=3, rounds=3)],
        [_exercise("burpees", sets=2, circuit=1, rounds=3)],
        [_exercise("burpees", sets=4, circuit=1, rounds=3)],
        [_exercise("burpees", sets=1, circuit=0, rounds=1)],
        [_exercise("burpees", sets=1, circuit=101, rounds=1)],
        [_exercise("burpees", sets=1, circuit=1, rounds=0)],
        [
            _exercise("burpees", sets=3, circuit=1, rounds=3),
            _exercise("squats", sets=4, circuit=1, rounds=4),
        ],
        [
            _exercise("burpees", sets=3, circuit=1, rounds=3),
            _exercise("plank", sets=1),
            _exercise("squats", sets=3, circuit=1, rounds=3),
        ],
        [
            _exercise("burpees", sets=3, circuit=1, rounds=3),
            _exercise("row", sets=2, circuit=2, rounds=2),
            _exercise("squats", sets=3, circuit=1, rounds=3),
        ],
    ],
    ids=[
        "circuit-without-rounds",
        "rounds-without-circuit",
        "fewer-sets-than-rounds",
        "more-sets-than-rounds",
        "circuit-below-1",
        "circuit-above-100",
        "rounds-below-1",
        "rounds-differ",
        "split-by-plain-entry",
        "split-by-another-circuit",
    ],
)
def test_a_circuit_that_doesnt_hold_together_is_refused(activities: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        GymPayload.model_validate({"activities": activities})


def test_two_circuits_side_by_side_are_kept_apart() -> None:
    payload = GymPayload.model_validate(
        {
            "activities": [
                _exercise("burpees", sets=3, circuit=1, rounds=3),
                _exercise("row", sets=2, circuit=2, rounds=2),
            ]
        }
    )

    assert [exercise.circuit for exercise in payload.activities] == [1, 2]
