"""The cycling plugin's draft edit (PRD-0004 F2, tickets #158 and #160): the athlete's distance
in km, moving time and elevation in metres replace each ride's own, an untouched value keeps
what was typed, and the result is validated by `CyclingPayload`."""

import pytest
from pydantic import JsonValue, ValidationError

from ai_trainer.domain.sports.cycling.edit import apply_cycling_edit
from ai_trainer.domain.sports.cycling.payload import CyclingPayload, Distance, Elevation, ZoneTime

_PAYLOAD: dict[str, JsonValue] = {
    "sport": "cycling",
    "activities": [
        {
            "distance": {"original": "40 miles", "value": 40, "unit": "mi", "km": 64.37},
            "moving_time_min": 105,
            "elevation": {"original": "2000 ft", "value": 2000, "unit": "ft", "m": 610},
            "zones": [{"zone": 2, "minutes": None}],
        },
        {"distance": None, "moving_time_min": 30, "elevation": None, "zones": []},
    ],
}


def _rides(*rows: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"activities": list(rows)}


def test_an_untouched_ride_keeps_what_was_typed_and_its_zones() -> None:
    edited = apply_cycling_edit(
        _PAYLOAD,
        _rides({"distance": "64.37", "moving_time_min": "105", "elevation": "610"}),
    )

    assert isinstance(edited, CyclingPayload)
    assert edited.model_dump(mode="json") == _PAYLOAD


def test_a_changed_distance_is_now_typed_in_km() -> None:
    edited = apply_cycling_edit(_PAYLOAD, _rides({"distance": " 70.5 "}))

    assert edited.activities[0].distance == Distance(
        original="70.5 km", value=70.5, unit="km", km=70.5
    )
    assert edited.activities[0].elevation is not None
    assert edited.activities[0].elevation.original == "2000 ft"


def test_a_tiny_typed_distance_is_kept_not_rejected() -> None:
    edited = apply_cycling_edit(_PAYLOAD, _rides({}, {"distance": "0.004"}))

    assert edited.activities[1].distance == Distance(
        original="0.004 km", value=0.004, unit="km", km=0
    )


def test_a_changed_elevation_is_now_typed_in_metres() -> None:
    edited = apply_cycling_edit(_PAYLOAD, _rides({}, {"elevation": "450"}))

    assert edited.activities[1].elevation == Elevation(original="450 m", value=450, unit="m", m=450)


def test_a_changed_moving_time_replaces_the_stored_one() -> None:
    edited = apply_cycling_edit(_PAYLOAD, _rides({"moving_time_min": "120"}))

    assert edited.activities[0].moving_time_min == 120
    assert edited.activities[0].zones == [ZoneTime(zone=2)]


def test_a_blank_field_clears_it() -> None:
    edited = apply_cycling_edit(
        _PAYLOAD, _rides({"distance": "", "moving_time_min": " ", "elevation": ""})
    )

    ride = edited.activities[0]
    assert (ride.distance, ride.moving_time_min, ride.elevation) == (None, None, None)


def test_rows_past_the_stored_rides_and_unknown_fields_are_ignored() -> None:
    edited = apply_cycling_edit(_PAYLOAD, _rides({"zones": "9"}, {}, {"distance": "not a number"}))

    assert edited.model_dump(mode="json") == _PAYLOAD


def test_an_edit_without_rows_changes_nothing() -> None:
    assert apply_cycling_edit(_PAYLOAD, {}).model_dump(mode="json") == _PAYLOAD


@pytest.mark.parametrize(
    ("row", "field"),
    [
        ({"distance": "far"}, "activities.1.distance"),
        ({"distance": "0"}, "activities.1.distance"),
        ({"distance": "-3"}, "activities.1.distance"),
        ({"elevation": "-1"}, "activities.1.elevation"),
        ({"elevation": "lots"}, "activities.1.elevation"),
        ({"moving_time_min": "0"}, "activities.1.moving_time_min"),
        ({"moving_time_min": "1441"}, "activities.1.moving_time_min"),
        ({"moving_time_min": "1.5"}, "activities.1.moving_time_min"),
    ],
)
def test_a_rejected_value_is_located_where_the_form_names_it(
    row: dict[str, JsonValue], field: str
) -> None:
    with pytest.raises(ValidationError) as raised:
        apply_cycling_edit(_PAYLOAD, _rides({}, row))

    assert {".".join(str(part) for part in error["loc"]) for error in raised.value.errors()} == {
        field
    }
