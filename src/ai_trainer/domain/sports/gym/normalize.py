"""Units (ADR-0006). Units are converted here in code, never by the model: it reports the number
and unit as typed, and code derives kg, seconds and metres. The original text of a load is kept
with only surrounding whitespace trimmed."""

from typing import Literal

from ai_trainer.domain.sports.gym.payload import Load, LoadUnit

KG_PER_LB = 0.45359237
SECONDS_PER_MINUTE = 60
METRES_PER_KM = 1000

type DurationUnit = Literal["s", "min"]
type DistanceUnit = Literal["m", "km"]


def to_kg(value: float, unit: LoadUnit) -> float:
    kg = value if unit == "kg" else value * KG_PER_LB
    return round(kg, 2)


def to_seconds(value: float, unit: DurationUnit) -> int:
    return round(value if unit == "s" else value * SECONDS_PER_MINUTE)


def to_metres(value: float, unit: DistanceUnit) -> float:
    return round(value if unit == "m" else value * METRES_PER_KM, 1)


def normalize_load(original: str, value: float, unit: LoadUnit) -> Load:
    return Load(original=original.strip(), value=value, unit=unit, kg=to_kg(value, unit))
