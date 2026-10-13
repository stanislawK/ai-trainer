"""Load units (ADR-0006). Units are converted here in code, never by the model: it reports the
number and unit as typed, and code derives kg. The original text is kept with only surrounding
whitespace trimmed."""

from ai_trainer.domain.sports.gym.payload import Load, LoadUnit

KG_PER_LB = 0.45359237


def to_kg(value: float, unit: LoadUnit) -> float:
    kg = value if unit == "kg" else value * KG_PER_LB
    return round(kg, 2)


def normalize_load(original: str, value: float, unit: LoadUnit) -> Load:
    return Load(original=original.strip(), value=value, unit=unit, kg=to_kg(value, unit))
