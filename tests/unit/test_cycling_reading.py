"""The cycling question (B17, ADR-0015, ticket #206): a ride distance `cycling.extract` can't
read safely asks km or miles; code turns the readings into options and enforces the pick. The
distance units of the athlete's recent rides are computed in code for the extraction's deps."""

from typing import Any

import pytest
from pydantic import ValidationError

from ai_trainer.domain.clarification import ChoiceOption
from ai_trainer.domain.sports.cycling.extraction import (
    CyclingExtraction,
    CyclingQuestion,
    recent_units,
)
from ai_trainer.domain.sports.registry import default_sport_registry


def _question(distance: str, *units: str) -> CyclingQuestion:
    return CyclingQuestion.model_validate({"distance": distance, "readings": list(units)})


def test_each_unit_becomes_an_option_with_the_distance_as_typed_likeliest_first() -> None:
    assert _question("40", "mi", "km").options() == (
        ChoiceOption(id="mi", copy_key="cycling_reading_mi", params={"distance": "40"}),
        ChoiceOption(id="km", copy_key="cycling_reading_km", params={"distance": "40"}),
    )


@pytest.mark.parametrize(
    "question",
    [
        {"distance": "40", "readings": ["km"]},
        {"distance": "40", "readings": ["km", "km"]},
        {"distance": "40", "readings": ["km", "furlong"]},
        {"distance": " ", "readings": ["km", "mi"]},
        {"readings": ["km", "mi"]},
        {"distance": "40 mi", "readings": ["km", "mi"]},
    ],
    ids=[
        "one unit",
        "the same unit twice",
        "not a unit",
        "a blank distance",
        "no distance",
        "a distance with its unit (skeptic finding)",
    ],
)
def test_a_question_needs_the_distance_and_two_distinct_units(question: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CyclingQuestion.model_validate(question)


def _session(*units: str | None) -> dict[str, Any]:
    return {
        "sport": "cycling",
        "activities": [
            {
                "distance": None
                if unit is None
                else {"original": f"40 {unit}", "value": 40, "unit": unit, "km": 40}
            }
            for unit in units
        ],
    }


def test_recent_units_are_each_once_most_recent_ride_first() -> None:
    assert recent_units([_session(None, "mi"), _session("km", "mi"), _session("km")]) == (
        "mi",
        "km",
    )


def test_no_recent_distances_give_no_units() -> None:
    assert recent_units([]) == ()
    assert recent_units([_session(None)]) == ()


def test_the_cycling_plugin_asks_and_reads_recent_units() -> None:
    spec = default_sport_registry().get("cycling").extraction

    assert spec is not None
    assert spec.question is not None
    assert spec.question.type is CyclingQuestion
    assert spec.question.since_version == 3
    assert spec.version >= spec.question.since_version
    assert spec.recent is recent_units


def _log(*distances: tuple[str, str | None] | None) -> CyclingExtraction:
    return CyclingExtraction.model_validate(
        {
            "occurred_on": "2026-10-02",
            "date_source": "assumed_today",
            "activities": [
                {
                    "distance": None
                    if distance is None
                    else {"original": distance[0], "value": 40, "unit": distance[1]}
                }
                for distance in distances
            ],
        }
    )


def test_a_distance_typed_without_a_unit_is_read_as_km() -> None:
    """All athletes are in Europe (owner-directed at #206's plan gate, 2026-10-21)."""
    [ride] = _log(("40", None)).to_payload().activities

    assert ride.distance is not None
    assert (ride.distance.original, ride.distance.unit, ride.distance.km) == ("40", "km", 40)


def test_an_answer_sets_the_unit_of_every_distance_typed_without_one() -> None:
    """The tap is enforced in code, not only in the prompt (ADR-0015, #164 amendment), also on
    a distance the resumed run gave a unit the message didn't."""
    answered = _log(("40", None), ("40", "km"), None, ("40 km", "km")).answered(
        _question("40", "km", "mi").options()[1]
    )

    assert [
        None if ride.distance is None else ride.distance.unit
        for ride in answered.to_payload().activities
    ] == ["mi", "mi", None, "km"]


@pytest.mark.parametrize(
    "answer",
    [
        ChoiceOption(id="climbing", copy_key="sport.climbing.label"),
        ChoiceOption(id="mi", copy_key="x"),
    ],
    ids=["a sport's option", "a unit without its distance"],
)
def test_an_option_that_is_no_reading_changes_nothing(answer: ChoiceOption) -> None:
    log = _log(("40", None))

    assert log.answered(answer) == log
