"""The climbing plugin: payload v1, grade scale detection and the extraction it is built from
(ADR-0006, ADR-0022, ticket #153)."""

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction
from ai_trainer.domain.sports.climbing.normalize import detect_scale, normalize_grade
from ai_trainer.domain.sports.climbing.payload import (
    CLIMBING_PAYLOAD_VERSION,
    ClimbingActivity,
    ClimbingKind,
    ClimbingPayload,
    Grade,
    GradeScale,
)


@pytest.mark.parametrize(
    ("original", "scale"),
    [
        ("6A", "font"),
        ("6A+", "font"),
        ("7C+", "font"),
        ("4", None),
        ("6a", "french"),
        ("6b+", "french"),
        ("9c", "french"),
        ("V4", "v"),
        ("v10", "v"),
        ("VB", "v"),
        ("V3+", "v"),
        ("V17", "v"),
        ("V0", "v"),
        ("5.9", "yds"),
        ("5.11a", "yds"),
        ("5.12d", "yds"),
        ("5.10+", "yds"),
        ("5.15d", "yds"),
        ("5.1", "yds"),
    ],
)
def test_grade_scale_is_detected_from_the_original_string(original: str, scale: str | None) -> None:
    assert detect_scale(original) == scale


@pytest.mark.parametrize(
    "original",
    [
        "blue",
        "purple circuit",
        "6c/+",
        "6Ab",
        "7a/7A",
        "V",
        # Non-ASCII digits are not grades (skeptic finding at #153).
        "V\u0663",
        "5.\u0661\u0662",
        "\uff17a",
        # Grades past the top of the scale do not exist.
        "V18",
        "V99",
        "5.16a",
        "5.99",
    ],
)
def test_unrecognised_grade_keeps_original_with_null_scale(original: str) -> None:
    grade = normalize_grade(original)

    assert grade == Grade(original=original, scale=None)
    assert grade.french is None
    assert grade.ordinal is None


def test_grade_original_is_kept_as_typed_apart_from_surrounding_space() -> None:
    assert normalize_grade(" 6A ") == Grade(original="6A", scale="font")


@pytest.mark.parametrize(
    ("original", "kind", "scale"),
    [
        ("7a", "boulder", "font"),
        ("7A", "boulder", "font"),
        ("7A", "lead", "french"),
        ("6c+", "top_rope", "french"),
        ("V5", "lead", "v"),
        ("5.11a", "boulder", "yds"),
        ("blue", "boulder", None),
    ],
)
def test_a_font_or_french_grade_takes_the_scale_of_its_kind(
    original: str, kind: ClimbingKind, scale: GradeScale | None
) -> None:
    """Athletes type the letter in either case; the kind says which scale (ticket #164)."""
    assert normalize_grade(original, kind) == Grade(original=original, scale=scale)


def test_m1_grades_leave_french_and_ordinal_null() -> None:
    grade = normalize_grade("6b+")

    assert (grade.original, grade.scale, grade.french, grade.ordinal) == (
        "6b+",
        "french",
        None,
        None,
    )


def _extraction(**overrides: Any) -> ClimbingExtraction:
    values: dict[str, Any] = {
        "occurred_on": "2026-09-16",
        "date_source": "assumed_today",
        "activities": [
            {"kind": "boulder", "count": 3, "grade": "6A"},
            {"kind": "boulder", "count": 2, "grade": "6B"},
        ],
    }
    return ClimbingExtraction.model_validate(values | overrides)


def test_extraction_to_payload_keeps_counts_and_font_scale() -> None:
    payload = _extraction().to_payload()

    assert payload.sport == "climbing"
    assert [(a.kind, a.count, a.grade) for a in payload.activities] == [
        ("boulder", 3, Grade(original="6A", scale="font")),
        ("boulder", 2, Grade(original="6B", scale="font")),
    ]


def test_extraction_to_payload_reads_a_lowercase_boulder_grade_as_font() -> None:
    payload = _extraction(activities=[{"kind": "boulder", "count": 3, "grade": "7a"}]).to_payload()

    assert payload.activities[0].grade == Grade(original="7a", scale="font")


def test_extraction_to_payload_keeps_an_unrecognised_grade_without_a_scale() -> None:
    extraction = _extraction(activities=[{"kind": "boulder", "count": 2, "grade": "blue"}])

    payload = extraction.to_payload()

    assert payload.activities[0].grade == Grade(original="blue", scale=None)


def test_extraction_to_payload_leaves_a_missing_grade_missing() -> None:
    payload = _extraction(activities=[{"kind": "lead", "count": 4, "grade": None}]).to_payload()

    assert payload.activities[0].grade is None


@pytest.mark.parametrize("blank", ["", "  "])
def test_extraction_treats_a_blank_grade_as_no_grade(blank: str) -> None:
    payload = _extraction(activities=[{"kind": "boulder", "count": 1, "grade": blank}]).to_payload()

    assert payload.activities[0].grade is None


def test_extraction_to_payload_keeps_style_attempts_and_route() -> None:
    payload = _extraction(
        activities=[
            {
                "kind": "lead",
                "count": 1,
                "grade": "7a",
                "style": "redpoint",
                "attempts": 4,
                "route": {"name": "Pruebas", "crag": "Rodellar"},
            }
        ]
    ).to_payload()

    activity = payload.activities[0]
    assert (activity.style, activity.attempts) == ("redpoint", 4)
    assert activity.route is not None
    assert (activity.route.name, activity.route.crag) == ("Pruebas", "Rodellar")


def test_extraction_dates_are_absolute() -> None:
    extraction = _extraction(occurred_on="2026-09-15", date_source="relative")

    assert extraction.occurred_on == date(2026, 9, 15)
    assert extraction.date_source == "relative"


def test_extraction_rejects_an_unknown_date_source() -> None:
    with pytest.raises(ValidationError):
        _extraction(date_source="yesterday")


def test_payload_is_version_one_and_tagged_climbing() -> None:
    assert CLIMBING_PAYLOAD_VERSION == 1
    payload = ClimbingPayload.model_validate(
        {"activities": [{"kind": "top_rope", "count": 5, "grade": None}]}
    )

    assert payload.model_dump(mode="json") == {
        "sport": "climbing",
        "activities": [
            {
                "kind": "top_rope",
                "count": 5,
                "grade": None,
                "style": None,
                "attempts": None,
                "route": None,
            }
        ],
    }


def test_payload_dumps_a_grade_with_all_four_fields() -> None:
    activity = ClimbingActivity(kind="boulder", count=1, grade=Grade(original="V4", scale="v"))

    assert activity.model_dump(mode="json")["grade"] == {
        "original": "V4",
        "scale": "v",
        "french": None,
        "ordinal": None,
    }


@pytest.mark.parametrize(
    "activity",
    [
        {"kind": "boulder", "count": 0},
        {"kind": "boulder", "count": 1, "attempts": 0},
        {"kind": "board", "count": 1},
        {"kind": "lead", "count": 1, "style": "dogged"},
        {"kind": "lead", "count": 1, "route": {"name": ""}},
    ],
)
def test_payload_rejects_invalid_activities(activity: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ClimbingPayload.model_validate({"activities": [activity]})


def test_payload_needs_at_least_one_activity() -> None:
    with pytest.raises(ValidationError):
        ClimbingPayload.model_validate({"activities": []})


def test_extraction_needs_at_least_one_activity() -> None:
    with pytest.raises(ValidationError):
        _extraction(activities=[])
