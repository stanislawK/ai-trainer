"""The onboarding part of the athlete profile (PRD-0007 B33, ADR-0028): age, height, weight and
the limits to plan around. Every field is optional. Units are converted here, in code, never in
a prompt (ADR-0028 invariant 6), and the profile keeps metric values and a birth year."""

import math
import re
from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from typing import assert_never

from pydantic import BaseModel, ConfigDict

MIN_AGE, MAX_AGE = 10, 100
MIN_HEIGHT_CM, MAX_HEIGHT_CM = 100, 250
MIN_WEIGHT_KG, MAX_WEIGHT_KG = 25, 300
MAX_LIMITS_LENGTH = 300

_CM_PER_INCH = 2.54
_KG_PER_LB = 0.45359237
_WHOLE_NUMBER = re.compile(r"[0-9]{1,3}")
_DECIMAL = re.compile(r"[0-9]{1,3}(?:\.[0-9])?")


class HeightUnit(StrEnum):
    CM = "cm"
    M = "m"
    FT_IN = "ft_in"


class WeightUnit(StrEnum):
    KG = "kg"
    LB = "lb"


class AboutYou(BaseModel):
    """What the summary shows and the athlete edits: an age, metric values and the limits."""

    model_config = ConfigDict(frozen=True)

    age: int | None = None
    height_cm: int | None = None
    weight_kg: float | None = None
    limits: str | None = None


class AthleteProfile(BaseModel):
    """The stored profile (`athlete_profiles`): the age as a birth year, so it never goes
    stale (ADR-0028)."""

    model_config = ConfigDict(frozen=True)

    birth_year: int | None = None
    height_cm: int | None = None
    weight_kg: float | None = None
    limits: str | None = None


class FieldProblem(StrEnum):
    """What is wrong with a value typed on the summary."""

    OUT_OF_RANGE = "out_of_range"
    UNKNOWN = "unknown"
    # A value that needs another one beside it: a goal's target date without the goal.
    REQUIRED = "required"


def _round_half_up(value: float, digits: int = 0) -> float:
    # `round()` rounds half to even, so 177.5 cm would become 178 but 176.5 cm 176.
    scale = 10.0**digits
    return math.floor(value * scale + 0.5) / scale


def height_cm(value: float, unit: HeightUnit, *, inches: float | None = None) -> int | None:
    """`value` in `unit` as whole centimetres, or `None` outside the bounds. For `ft_in`,
    `value` is the feet and `inches` the rest."""
    match unit:
        case HeightUnit.CM:
            cm = value
        case HeightUnit.M:
            cm = value * 100
        case HeightUnit.FT_IN:
            cm = (value * 12 + (inches or 0)) * _CM_PER_INCH
        case _:
            assert_never(unit)
    whole = int(_round_half_up(cm))
    return whole if MIN_HEIGHT_CM <= whole <= MAX_HEIGHT_CM else None


def weight_kg(value: float, unit: WeightUnit) -> float | None:
    """`value` in `unit` as kilograms, or `None` outside the bounds. Kilograms keep one
    decimal; pounds become whole kilograms, the precision a pound figure carries."""
    match unit:
        case WeightUnit.KG:
            kg = _round_half_up(value, 1)
        case WeightUnit.LB:
            kg = _round_half_up(value * _KG_PER_LB)
        case _:
            assert_never(unit)
    return kg if MIN_WEIGHT_KG <= kg <= MAX_WEIGHT_KG else None


def bounded_age(age: int) -> int | None:
    return age if MIN_AGE <= age <= MAX_AGE else None


def about_you_from_read(parsed: Mapping[str, object]) -> AboutYou:
    """What a read stored, as the summary shows it: a value of the wrong type or outside the
    bounds stays empty, as a value the answer left unclear does (B34)."""

    def number(key: str) -> float | None:
        value = parsed.get(key)
        # `bool` is an `int`; a read never means a number by it.
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return value

    age, height, weight = number("age"), number("height_cm"), number("weight_kg")
    limits = parsed.get("limits")
    return AboutYou(
        age=bounded_age(int(age)) if age is not None and age == int(age) else None,
        height_cm=int(height)
        if height is not None and MIN_HEIGHT_CM <= height <= MAX_HEIGHT_CM and height == int(height)
        else None,
        weight_kg=_round_half_up(weight, 1)
        if weight is not None and MIN_WEIGHT_KG <= weight <= MAX_WEIGHT_KG
        else None,
        limits=bounded_limits(limits) if isinstance(limits, str) else None,
    )


def bounded_limits(limits: str) -> str | None:
    """The limits trimmed, cut to the column's length; `None` when blank."""
    text = limits.replace("\x00", "").strip()[:MAX_LIMITS_LENGTH].strip()
    return text or None


def birth_year_for(age: int, today: date) -> int:
    """The birth year an age stated `today` gives; the birthday itself is never asked."""
    return today.year - age


def age_on(birth_year: int, today: date) -> int:
    return today.year - birth_year


def profile_from(about_you: AboutYou, *, today: date) -> AthleteProfile:
    return AthleteProfile(
        birth_year=birth_year_for(about_you.age, today) if about_you.age is not None else None,
        height_cm=about_you.height_cm,
        weight_kg=about_you.weight_kg,
        limits=about_you.limits,
    )


def about_you_from_profile(profile: AthleteProfile, *, today: date) -> AboutYou:
    return AboutYou(
        age=age_on(profile.birth_year, today) if profile.birth_year is not None else None,
        height_cm=profile.height_cm,
        weight_kg=profile.weight_kg,
        limits=profile.limits,
    )


def typed_whole(raw: str, low: int, high: int) -> int | FieldProblem | None:
    """A whole number typed on the summary: `None` when blank, a problem outside the bounds."""
    text = raw.strip()
    if not text:
        return None
    # ASCII digits only: `int()` alone also takes "+5", "1_0" and non-ASCII digits.
    if not _WHOLE_NUMBER.fullmatch(text) or not low <= int(text) <= high:
        return FieldProblem.OUT_OF_RANGE
    return int(text)


def typed_decimal(raw: str, low: int, high: int) -> float | FieldProblem | None:
    """A number with at most one decimal, after a point or a comma (PRD 0008 G15)."""
    text = raw.strip().replace(",", ".")
    if not text:
        return None
    # `float()` alone also takes "nan", "inf" and "1e2".
    if not _DECIMAL.fullmatch(text) or not low <= float(text) <= high:
        return FieldProblem.OUT_OF_RANGE
    return float(text)


def parse_about_you(form: Mapping[str, str]) -> AboutYou | dict[str, FieldProblem]:
    """The summary's About you fields as typed (B35): blank is empty, and every field outside
    its bounds is named at once, so the form can flag each one."""
    raw_limits = form.get("limits", "").replace("\x00", "").strip()
    values = {
        "age": typed_whole(form.get("age", ""), MIN_AGE, MAX_AGE),
        "height_cm": typed_whole(form.get("height_cm", ""), MIN_HEIGHT_CM, MAX_HEIGHT_CM),
        "weight_kg": typed_decimal(form.get("weight_kg", ""), MIN_WEIGHT_KG, MAX_WEIGHT_KG),
        "limits": FieldProblem.OUT_OF_RANGE
        if len(raw_limits) > MAX_LIMITS_LENGTH
        else raw_limits or None,
    }
    problems = {field: value for field, value in values.items() if isinstance(value, FieldProblem)}
    if problems:
        return problems
    return AboutYou.model_validate(values)
