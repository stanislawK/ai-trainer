"""The onboarding part of the athlete profile: units converted and bounded in code, age kept as
a birth year (PRD-0007 B33, B34, ADR-0028 invariant 6, ticket #254)."""

from datetime import date

import pytest

from ai_trainer.domain.athlete_profile import (
    AboutYou,
    FieldProblem,
    HeightUnit,
    WeightUnit,
    about_you_from_profile,
    about_you_from_read,
    age_on,
    birth_year_for,
    height_cm,
    parse_about_you,
    profile_from,
    weight_kg,
)


def test_feet_and_inches_become_whole_centimetres() -> None:
    assert height_cm(5, HeightUnit.FT_IN, inches=10) == 178


def test_feet_alone_count_no_inches() -> None:
    assert height_cm(6, HeightUnit.FT_IN) == 183


def test_metres_become_centimetres() -> None:
    assert height_cm(1.78, HeightUnit.M) == 178


def test_centimetres_round_to_whole_ones() -> None:
    assert height_cm(177.6, HeightUnit.CM) == 178


@pytest.mark.parametrize(
    ("value", "unit"), [(99, HeightUnit.CM), (251, HeightUnit.CM), (3, HeightUnit.M)]
)
def test_a_height_outside_the_bounds_stays_empty(value: float, unit: HeightUnit) -> None:
    assert height_cm(value, unit) is None


def test_pounds_become_whole_kilograms() -> None:
    assert weight_kg(160, WeightUnit.LB) == 73


def test_kilograms_keep_one_decimal() -> None:
    assert weight_kg(72.46, WeightUnit.KG) == 72.5


@pytest.mark.parametrize(
    ("value", "unit"), [(24, WeightUnit.KG), (301, WeightUnit.KG), (700, WeightUnit.LB)]
)
def test_a_weight_outside_the_bounds_stays_empty(value: float, unit: WeightUnit) -> None:
    assert weight_kg(value, unit) is None


def test_age_is_stored_as_a_birth_year_and_shown_as_an_age_again() -> None:
    today = date(2026, 10, 24)
    year = birth_year_for(31, today)
    assert year == 1995
    assert age_on(year, today) == 31


def test_the_profile_keeps_the_birth_year_and_the_metric_values() -> None:
    profile = profile_from(
        AboutYou(age=31, height_cm=178, weight_kg=73, limits="Sore left shoulder"),
        today=date(2026, 10, 24),
    )
    assert profile.birth_year == 1995
    assert profile.height_cm == 178
    assert profile.weight_kg == 73
    assert profile.limits == "Sore left shoulder"
    assert about_you_from_profile(profile, today=date(2026, 10, 24)).age == 31


def test_an_empty_about_you_makes_an_empty_profile() -> None:
    profile = profile_from(AboutYou(), today=date(2026, 10, 24))
    assert profile.birth_year is None
    assert about_you_from_profile(profile, today=date(2026, 10, 24)) == AboutYou()


def test_the_summary_form_parses_typed_values() -> None:
    parsed = parse_about_you(
        {"age": " 31 ", "height_cm": "178", "weight_kg": "72.5", "limits": " Sore shoulder "}
    )
    assert parsed == AboutYou(age=31, height_cm=178, weight_kg=72.5, limits="Sore shoulder")


def test_blank_summary_fields_stay_empty() -> None:
    assert parse_about_you({"age": "", "height_cm": " ", "weight_kg": "", "limits": ""}) == (
        AboutYou()
    )


def test_a_decimal_comma_is_read_as_a_point() -> None:
    assert parse_about_you({"weight_kg": "72,5"}) == AboutYou(weight_kg=72.5)


@pytest.mark.parametrize(
    ("field", "raw"),
    [
        ("age", "9"),
        ("age", "101"),
        ("age", "thirty"),
        ("age", "31.5"),
        ("height_cm", "99"),
        ("height_cm", "251"),
        ("height_cm", "1e2"),
        ("weight_kg", "24.9"),
        ("weight_kg", "300.1"),
        ("weight_kg", "nan"),
        ("limits", "x" * 301),
    ],
)
def test_a_summary_value_outside_its_bounds_names_the_field(field: str, raw: str) -> None:
    problems = parse_about_you({field: raw})
    assert problems == {field: FieldProblem.OUT_OF_RANGE}


def test_every_bad_summary_field_is_named_at_once() -> None:
    assert parse_about_you({"age": "5", "height_cm": "20"}) == {
        "age": FieldProblem.OUT_OF_RANGE,
        "height_cm": FieldProblem.OUT_OF_RANGE,
    }


def test_limits_lose_a_nul_a_hand_crafted_request_sends() -> None:
    assert parse_about_you({"limits": "Knee\x00"}) == AboutYou(limits="Knee")


def test_a_read_shows_its_values_within_the_bounds() -> None:
    assert about_you_from_read(
        {"age": 31, "height_cm": 178, "weight_kg": 72.46, "limits": " Sore shoulder "}
    ) == AboutYou(age=31, height_cm=178, weight_kg=72.5, limits="Sore shoulder")


@pytest.mark.parametrize(
    "parsed",
    [
        {"age": 7, "height_cm": 300, "weight_kg": 500, "limits": "   "},
        {"age": True, "height_cm": "178", "weight_kg": None, "limits": 3},
        {"age": 31.5, "height_cm": 177.5},
        {},
    ],
)
def test_a_read_value_of_the_wrong_type_or_out_of_bounds_stays_empty(
    parsed: dict[str, object],
) -> None:
    assert about_you_from_read(parsed) == AboutYou()


def test_a_read_limit_longer_than_the_column_is_cut_to_it() -> None:
    assert about_you_from_read({"limits": "x" * 400}).limits == "x" * 300
