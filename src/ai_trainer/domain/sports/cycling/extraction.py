"""What `cycling.extract` returns (ADR-0008, ADR-0014): an absolute date, how it was found,
and each ride with its distance and elevation exactly as typed. The model copies the numbers
and text and picks the unit; code, not the model, converts to km and metres. A ride distance it
can't read safely is a question instead: km or miles, likeliest first (B17, ticket #206)."""

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Self, get_args

from pydantic import BaseModel, Field, JsonValue, StringConstraints, field_validator

from ai_trainer.domain.clarification import ChoiceOption
from ai_trainer.domain.sports.base import ExtractionQuestion, SessionExtraction
from ai_trainer.domain.sports.cycling.normalize import normalize_distance, normalize_elevation
from ai_trainer.domain.sports.cycling.payload import (
    CyclingPayload,
    DistanceUnit,
    ElevationUnit,
    Ride,
    ZoneTime,
)

# A distance typed with its unit has letters in it ("62 km", "40mi", "25 mil"); "40" has none.
_UNIT_TEXT = re.compile(r"[^\W\d_]")


class ExtractedDistance(BaseModel):
    original: str = Field(min_length=1)
    value: float = Field(gt=0)
    # None when the message gives no unit ("rode 40") and the athlete's recent rides don't
    # settle it; it is read as km, owner-directed at #206's plan gate (all athletes are in
    # Europe), and the athlete can change it before confirming.
    unit: DistanceUnit | None = None


class ExtractedElevation(BaseModel):
    original: str = Field(min_length=1)
    value: float = Field(ge=0)
    unit: ElevationUnit


class ExtractedRide(BaseModel):
    distance: ExtractedDistance | None = None
    moving_time_min: int | None = Field(default=None, ge=1, le=1440)
    elevation: ExtractedElevation | None = None
    zones: list[ZoneTime] = Field(default_factory=list)


# A reading's option copy, in `components/cycling_copy.html`, is this plus its unit.
_READING_COPY_KEY = "cycling_reading_"


class CyclingExtraction(SessionExtraction):
    activities: list[ExtractedRide] = Field(min_length=1)

    def answered(self, answer: ChoiceOption) -> Self:
        """The picked unit holds for every distance typed without one, also where the resumed
        run gave it one; every other ride stays as the message said. An option that is no
        reading changes nothing."""
        if answer.id not in get_args(DistanceUnit.__value__) or "distance" not in answer.params:
            return self
        return self.model_copy(
            update={
                "activities": [
                    ride.model_copy(
                        update={"distance": ride.distance.model_copy(update={"unit": answer.id})}
                    )
                    if ride.distance is not None and not _UNIT_TEXT.search(ride.distance.original)
                    else ride
                    for ride in self.activities
                ]
            }
        )

    def to_payload(self) -> CyclingPayload:
        return CyclingPayload(
            activities=[
                Ride(
                    distance=(
                        normalize_distance(
                            ride.distance.original, ride.distance.value, ride.distance.unit or "km"
                        )
                        if ride.distance is not None
                        else None
                    ),
                    moving_time_min=ride.moving_time_min,
                    elevation=(
                        normalize_elevation(
                            ride.elevation.original, ride.elevation.value, ride.elevation.unit
                        )
                        if ride.elevation is not None
                        else None
                    ),
                    zones=list(ride.zones),
                )
                for ride in self.activities
            ]
        )


class CyclingQuestion(ExtractionQuestion):
    """`distance` is the ride's distance as typed, without a unit; `readings` the units it may
    be in."""

    # No `pattern`: a model's constrained decoding reads it as the whole string, so `\S` gave
    # one character (seen in #206's first eval run).
    distance: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    readings: list[DistanceUnit] = Field(min_length=2)

    @field_validator("distance")
    @classmethod
    def _typed_without_a_unit(cls, distance: str) -> str:
        if _UNIT_TEXT.search(distance):
            raise ValueError("the question is about a distance typed without a unit")
        return distance

    @field_validator("readings")
    @classmethod
    def _one_reading_per_unit(cls, readings: list[DistanceUnit]) -> list[DistanceUnit]:
        # The unit is the option's ID, which the athlete's tap sends back (ADR-0015 invariant 4).
        if len(set(readings)) != len(readings):
            raise ValueError("each reading needs a unit of its own")
        return readings

    def options(self) -> tuple[ChoiceOption, ...]:
        return tuple(
            ChoiceOption(
                id=unit,
                copy_key=f"{_READING_COPY_KEY}{unit}",
                params={"distance": self.distance},
            )
            for unit in self.readings
        )


def recent_units(payloads: Sequence[Mapping[str, JsonValue]]) -> tuple[str, ...]:
    """The distance units of the athlete's recent rides, each once, from the most recent
    session first (ticket #206)."""
    units: dict[str, None] = {}
    for payload in payloads:
        for ride in CyclingPayload.model_validate(payload).activities:
            if ride.distance is not None:
                units.setdefault(ride.distance.unit)
    return tuple(units)
