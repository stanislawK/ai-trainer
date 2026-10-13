"""Distance and elevation units (ADR-0006). Units are converted here in code, never by the
model: it reports the number and unit as typed, and code derives km and metres. The original
text is kept with only surrounding whitespace trimmed."""

from ai_trainer.domain.sports.cycling.payload import (
    Distance,
    DistanceUnit,
    Elevation,
    ElevationUnit,
)

KM_PER_MILE = 1.609344
M_PER_FOOT = 0.3048


def to_km(value: float, unit: DistanceUnit) -> float:
    km = value if unit == "km" else value * KM_PER_MILE
    return round(km, 2)


def to_m(value: float, unit: ElevationUnit) -> int:
    m = value if unit == "m" else value * M_PER_FOOT
    return round(m)


def normalize_distance(original: str, value: float, unit: DistanceUnit) -> Distance:
    return Distance(original=original.strip(), value=value, unit=unit, km=to_km(value, unit))


def normalize_elevation(original: str, value: float, unit: ElevationUnit) -> Elevation:
    return Elevation(original=original.strip(), value=value, unit=unit, m=to_m(value, unit))
