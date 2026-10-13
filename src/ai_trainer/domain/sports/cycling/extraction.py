"""What `cycling.extract` returns (ADR-0008, ADR-0014): an absolute date, how it was found,
and each ride with its distance and elevation exactly as typed. The model copies the numbers
and text and picks the unit; code, not the model, converts to km and metres."""

from pydantic import BaseModel, Field

from ai_trainer.domain.sports.base import SessionExtraction
from ai_trainer.domain.sports.cycling.normalize import normalize_distance, normalize_elevation
from ai_trainer.domain.sports.cycling.payload import (
    CyclingPayload,
    DistanceUnit,
    ElevationUnit,
    Ride,
    ZoneTime,
)


class ExtractedDistance(BaseModel):
    original: str = Field(min_length=1)
    value: float = Field(gt=0)
    unit: DistanceUnit


class ExtractedElevation(BaseModel):
    original: str = Field(min_length=1)
    value: float = Field(ge=0)
    unit: ElevationUnit


class ExtractedRide(BaseModel):
    distance: ExtractedDistance | None = None
    moving_time_min: int | None = Field(default=None, ge=1, le=1440)
    elevation: ExtractedElevation | None = None
    zones: list[ZoneTime] = Field(default_factory=list)


class CyclingExtraction(SessionExtraction):
    activities: list[ExtractedRide] = Field(min_length=1)

    def to_payload(self) -> CyclingPayload:
        return CyclingPayload(
            activities=[
                Ride(
                    distance=(
                        normalize_distance(
                            ride.distance.original, ride.distance.value, ride.distance.unit
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
