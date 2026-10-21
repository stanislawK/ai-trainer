"""The cycling plugin: payload v1, unit normalization and the extraction it is built from
(ADR-0006, ADR-0022, ticket #160)."""

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from ai_trainer.domain.sports.cycling.edit import apply_cycling_edit
from ai_trainer.domain.sports.cycling.extraction import CyclingExtraction
from ai_trainer.domain.sports.cycling.normalize import (
    normalize_distance,
    normalize_elevation,
    to_km,
    to_m,
)
from ai_trainer.domain.sports.cycling.payload import (
    CYCLING_PAYLOAD_VERSION,
    CyclingPayload,
    Distance,
    Elevation,
    Ride,
    ZoneTime,
)
from ai_trainer.domain.sports.registry import default_sport_registry


@pytest.mark.parametrize(
    ("value", "unit", "km"),
    [(62, "km", 62.0), (40, "mi", 64.37), (1, "mi", 1.61), (25.5, "km", 25.5), (100, "mi", 160.93)],
)
def test_a_distance_is_converted_to_km_in_code(value: float, unit: Any, km: float) -> None:
    assert to_km(value, unit) == km


@pytest.mark.parametrize(
    ("value", "unit", "m"),
    [(800, "m", 800), (2000, "ft", 610), (1000, "ft", 305), (0, "m", 0), (812.6, "m", 813)],
)
def test_an_elevation_is_converted_to_whole_metres_in_code(value: float, unit: Any, m: int) -> None:
    assert to_m(value, unit) == m


def test_a_distance_in_miles_is_stored_in_km_with_its_original_text() -> None:
    assert normalize_distance(" 40 miles ", 40, "mi") == Distance(
        original="40 miles", value=40, unit="mi", km=64.37
    )


def test_a_tiny_distance_that_rounds_to_0_km_is_still_kept() -> None:
    # Skeptic finding at #160: `km` once had to be above 0, so this raised.
    assert normalize_distance("0.003 mi", 0.003, "mi").km == 0


def test_an_elevation_in_feet_is_stored_in_metres_with_its_original_text() -> None:
    assert normalize_elevation("2000 ft", 2000, "ft") == Elevation(
        original="2000 ft", value=2000, unit="ft", m=610
    )


def _extraction(**overrides: Any) -> CyclingExtraction:
    values: dict[str, Any] = {
        "occurred_on": "2026-10-02",
        "date_source": "relative",
        "activities": [
            {
                "distance": {"original": "62 km", "value": 62, "unit": "km"},
                "elevation": {"original": "800 m", "value": 800, "unit": "m"},
            }
        ],
    }
    return CyclingExtraction.model_validate(values | overrides)


def test_62_km_with_800_m_climbing_becomes_a_ride_in_km_and_metres() -> None:
    payload = _extraction().to_payload()

    assert payload.sport == "cycling"
    (ride,) = payload.activities
    assert ride.distance == Distance(original="62 km", value=62, unit="km", km=62)
    assert ride.elevation == Elevation(original="800 m", value=800, unit="m", m=800)
    assert (ride.moving_time_min, ride.zones) == (None, [])


def test_two_hours_easy_is_a_ride_of_120_minutes_and_nothing_else() -> None:
    payload = _extraction(
        date_source="assumed_today", activities=[{"moving_time_min": 120}]
    ).to_payload()

    assert payload.activities == [Ride(moving_time_min=120)]


def test_a_ride_in_miles_and_feet_keeps_what_was_typed_beside_km_and_metres() -> None:
    payload = _extraction(
        activities=[
            {
                "distance": {"original": "40 miles", "value": 40, "unit": "mi"},
                "elevation": {"original": "2000 ft", "value": 2000, "unit": "ft"},
            }
        ]
    ).to_payload()

    (ride,) = payload.activities
    assert ride.distance is not None
    assert ride.elevation is not None
    assert (ride.distance.original, ride.distance.km) == ("40 miles", 64.37)
    assert (ride.elevation.original, ride.elevation.m) == ("2000 ft", 610)


def test_zones_and_several_rides_are_kept_in_order() -> None:
    payload = _extraction(
        activities=[
            {"moving_time_min": 30, "zones": [{"zone": 2}]},
            {"moving_time_min": 60, "zones": [{"zone": 4, "minutes": 20}, {"zone": 2}]},
        ]
    ).to_payload()

    assert [ride.moving_time_min for ride in payload.activities] == [30, 60]
    assert payload.activities[1].zones == [ZoneTime(zone=4, minutes=20), ZoneTime(zone=2)]


def test_extraction_dates_are_absolute() -> None:
    extraction = _extraction()

    assert (extraction.occurred_on, extraction.date_source) == (date(2026, 10, 2), "relative")


@pytest.mark.parametrize(
    "ride",
    [
        {"distance": {"original": "0 km", "value": 0, "unit": "km"}},
        {"distance": {"original": "62 leagues", "value": 62, "unit": "leagues"}},
        {"distance": {"original": "", "value": 62, "unit": "km"}},
        {"elevation": {"original": "-5 m", "value": -5, "unit": "m"}},
        {"elevation": {"original": "800 yd", "value": 800, "unit": "yd"}},
        {"moving_time_min": 0},
        {"moving_time_min": 1441},
        {"zones": [{"zone": 0}]},
        {"zones": [{"zone": 8}]},
        {"zones": [{"zone": 2, "minutes": 0}]},
    ],
)
def test_extraction_rejects_invalid_rides(ride: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _extraction(activities=[ride])


def test_extraction_needs_at_least_one_ride() -> None:
    with pytest.raises(ValidationError):
        _extraction(activities=[])


def test_payload_is_version_one_and_tagged_cycling() -> None:
    assert CYCLING_PAYLOAD_VERSION == 1
    payload = CyclingPayload.model_validate({"activities": [{"moving_time_min": 120}]})

    assert payload.model_dump(mode="json") == {
        "sport": "cycling",
        "activities": [{"distance": None, "moving_time_min": 120, "elevation": None, "zones": []}],
    }


@pytest.mark.parametrize(
    "ride",
    [
        {"distance": {"original": "40 miles", "value": 40, "unit": "mi", "km": -1}},
        {"elevation": {"original": "800 m", "value": 800, "unit": "m", "m": -1}},
        {"moving_time_min": 0},
    ],
)
def test_payload_rejects_invalid_rides(ride: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CyclingPayload.model_validate({"activities": [ride]})


def test_payload_needs_at_least_one_ride() -> None:
    with pytest.raises(ValidationError):
        CyclingPayload.model_validate({"activities": []})


def test_the_registry_wires_cycling_to_its_extraction_template_and_payload() -> None:
    plugin = default_sport_registry().get("cycling")

    assert (plugin.icon, plugin.label_key) == ("bike", "sport.cycling.label")
    assert plugin.extraction is not None
    assert (plugin.extraction.template_id, plugin.extraction.version) == ("cycling.extract", 3)
    assert plugin.extraction.output_type is CyclingExtraction
    assert plugin.extraction.payload_type is CyclingPayload
    assert plugin.extraction.payload_version == CYCLING_PAYLOAD_VERSION
    assert plugin.extraction.apply_edit is apply_cycling_edit
