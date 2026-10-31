"""Gym payload v1 read into the typed rows of ADR-0032 (ticket #376): one entry per exercise,
one set row per set, and a value the message didn't give left empty (B38). The unit hint of a
recent exercise is read back from the rows."""

from decimal import Decimal
from typing import Any

import pytest

from ai_trainer.domain.sports.gym.payload import GymPayload
from ai_trainer.domain.sports.gym.rows import GymEntryRow, GymSetRow, entries_of, unit_of


def _payload(*activities: dict[str, Any]) -> GymPayload:
    return GymPayload.model_validate({"activities": list(activities)})


def _load(original: str, value: float, unit: str, kg: float) -> dict[str, Any]:
    return {"original": original, "value": value, "unit": unit, "kg": kg}


def test_three_sets_of_eight_at_45_kg_are_three_equal_set_rows() -> None:
    payload = _payload(
        {"name": "Bench press", "sets": 3, "reps": 8, "load": _load("45kg", 45, "kg", 45)}
    )

    assert entries_of(payload) == (
        GymEntryRow(
            name_as_typed="Bench press",
            measure="load_reps",
            rir=None,
            sets=tuple(
                GymSetRow(set_no=n, reps=8, load_kg=Decimal("45"), load_original="45kg")
                for n in (1, 2, 3)
            ),
        ),
    )


def test_a_value_the_message_did_not_give_maps_to_none() -> None:
    (entry,) = entries_of(_payload({"name": "Pull-ups", "sets": 4}))

    assert (entry.measure, entry.rir) == ("reps", None)
    assert entry.sets == tuple(
        GymSetRow(set_no=n, reps=None, load_kg=None, load_original=None) for n in (1, 2, 3, 4)
    )


def test_an_exercise_without_a_set_count_is_one_set_with_what_was_given() -> None:
    # Owner-chosen at #376's plan gate (D1): rows can't say "an unknown number of sets".
    (entry,) = entries_of(
        _payload({"name": "Squat", "reps": 5, "rir": 2, "load": _load("225 lb", 225, "lb", 102.06)})
    )

    assert (entry.measure, entry.rir) == ("load_reps", 2)
    assert entry.sets == (
        GymSetRow(set_no=1, reps=5, load_kg=Decimal("102.06"), load_original="225 lb"),
    )


def test_each_exercise_is_its_own_entry_in_the_order_logged() -> None:
    entries = entries_of(_payload({"name": "Squat", "sets": 1}, {"name": "Dips", "sets": 1}))

    assert [entry.name_as_typed for entry in entries] == ["Squat", "Dips"]


@pytest.mark.parametrize(
    ("original", "kg", "unit"),
    [
        ("45kg", "45", "kg"),
        ("45", "45", "kg"),
        ("22,5 kg", "22.5", "kg"),
        ("225 lb", "102.06", "lb"),
        ("100", "45.36", "lb"),
        ("60 funtów", "27.22", "lb"),
        ("ciężko", "40", None),
        ("50", "40", None),
    ],
)
def test_a_loads_unit_is_read_back_from_its_typed_number_and_kg(
    original: str, kg: str, unit: str | None
) -> None:
    # Owner-chosen at #376's plan gate (D2): the rows keep kg and the text, not the unit.
    assert unit_of(original, Decimal(kg)) == unit


def test_v1_payload_gives_the_rows_it_gave_before() -> None:
    # AC #395: v1 is read through the upcaster, and no v1 value moves.
    payload = _payload(
        {"name": "Squat", "sets": 2, "reps": 5, "load": _load("100kg", 100, "kg", 100), "rir": 1},
        {"name": "Dips", "reps": 12},
        {"name": "Plank"},
    )

    assert entries_of(payload) == (
        GymEntryRow(
            name_as_typed="Squat",
            measure="load_reps",
            rir=1,
            sets=tuple(
                GymSetRow(set_no=n, reps=5, load_kg=Decimal("100"), load_original="100kg")
                for n in (1, 2)
            ),
        ),
        GymEntryRow(
            name_as_typed="Dips",
            measure="reps",
            rir=None,
            sets=(GymSetRow(set_no=1, reps=12, load_kg=None, load_original=None),),
        ),
        GymEntryRow(
            name_as_typed="Plank",
            measure="reps",
            rir=None,
            sets=(GymSetRow(set_no=1, reps=None, load_kg=None, load_original=None),),
        ),
    )


def test_plank_three_sets_of_45_s_is_time() -> None:
    payload = _payload(
        {
            "name_as_typed": "plank",
            "measure": "time",
            "sets": [{"duration_s": 45}, {"duration_s": 45}, {"duration_s": 45}],
        }
    )

    (entry,) = entries_of(payload)

    assert entry.measure == "time"
    assert [row.duration_s for row in entry.sets] == [45, 45, 45]
    assert [row.set_no for row in entry.sets] == [1, 2, 3]


def test_a_v2_entry_carries_its_side_implements_band_and_distance() -> None:
    (entry,) = entries_of(
        _payload(
            {
                "name_as_typed": "carry",
                "measure": "distance",
                "per_side": True,
                "implements": 2,
                "sets": [{"distance_m": 40.5, "band_label": "red"}],
            }
        )
    )

    assert (entry.per_side, entry.implements) == (True, 2)
    assert entry.sets[0].distance_m == Decimal("40.5")
    assert entry.sets[0].band_label == "red"
