"""Cycling payload v1 (ADR-0006, ADR-0022). Only this model reads or writes a cycling
session's `payload`; a change bumps `CYCLING_PAYLOAD_VERSION` and older versions are upcast
on read (ADR-0022 invariant 4)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CYCLING_PAYLOAD_VERSION = 1

type DistanceUnit = Literal["km", "mi"]
type ElevationUnit = Literal["m", "ft"]

# Power zones run 1-7; heart-rate zones 1-5 fit inside.
MAX_ZONE = 7


class Distance(BaseModel):
    """A distance as the athlete typed it. `original`, `value` and `unit` are the truth;
    `km` is derived by code, never by the model."""

    model_config = ConfigDict(frozen=True)

    original: str = Field(min_length=1)
    value: float = Field(gt=0)
    unit: DistanceUnit
    # A tiny typed distance can round to 0 km; only what was typed must be above 0.
    km: float = Field(ge=0)


class Elevation(BaseModel):
    """Elevation gain as the athlete typed it. `m` is derived by code, never by the model."""

    model_config = ConfigDict(frozen=True)

    original: str = Field(min_length=1)
    value: float = Field(ge=0)
    unit: ElevationUnit
    m: int = Field(ge=0)


class ZoneTime(BaseModel):
    """Time spent in one training zone."""

    zone: int = Field(ge=1, le=MAX_ZONE)
    minutes: int | None = Field(default=None, ge=1, le=1440)


class Ride(BaseModel):
    distance: Distance | None = None
    moving_time_min: int | None = Field(default=None, ge=1, le=1440)
    elevation: Elevation | None = None
    zones: list[ZoneTime] = []


class CyclingPayload(BaseModel):
    sport: Literal["cycling"] = "cycling"
    activities: list[Ride] = Field(min_length=1)
