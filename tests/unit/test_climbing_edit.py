"""The climbing plugin's draft edit (PRD-0004 F2, ticket #158): the athlete's count, grade and
style replace each activity's own, the grade's scale comes from code, and the result is
validated by `ClimbingPayload`."""

import pytest
from pydantic import JsonValue, ValidationError

from ai_trainer.domain.sports.climbing.edit import apply_climbing_edit
from ai_trainer.domain.sports.climbing.payload import ClimbingPayload, Grade
from ai_trainer.domain.sports.registry import default_sport_registry
from tests.chat_support import THREE_6A_TWO_6B

_PAYLOAD = THREE_6A_TWO_6B.to_payload().model_dump(mode="json")


def _activities(*rows: tuple[str, str, str]) -> dict[str, JsonValue]:
    return {
        "activities": [
            {"count": count, "grade": grade, "style": style} for count, grade, style in rows
        ]
    }


def test_editing_a_grade_from_6b_to_6c_keeps_scale_font() -> None:
    edited = apply_climbing_edit(_PAYLOAD, _activities(("3", "6A", ""), ("2", "6C", "")))

    assert isinstance(edited, ClimbingPayload)
    assert edited.activities[1].grade == Grade(original="6C", scale="font")


def test_an_edited_grade_gets_its_scale_from_what_was_typed_and_the_kind() -> None:
    """A boulder's grade typed in lowercase is still Font (ticket #164)."""
    edited = apply_climbing_edit(_PAYLOAD, _activities(("3", " 6a+ ", ""), ("2", "7-", "")))

    assert edited.activities[0].grade == Grade(original="6a+", scale="font")
    assert edited.activities[1].grade == Grade(original="7-", scale=None)


def test_an_edited_route_grade_typed_in_capitals_is_french() -> None:
    payload: dict[str, JsonValue] = {
        "sport": "climbing",
        "activities": [{"kind": "lead", "count": 1, "grade": None}],
    }

    edited = apply_climbing_edit(payload, _activities(("1", "7A", "")))

    assert edited.activities[0].grade == Grade(original="7A", scale="french")


def test_an_unchanged_grade_is_kept_as_it_was_stored() -> None:
    payload: dict[str, JsonValue] = {
        "sport": "climbing",
        "activities": [
            {
                "kind": "lead",
                "count": 1,
                "grade": {"original": "8a", "scale": "french", "french": "8a", "ordinal": 30},
            }
        ],
    }

    edited = apply_climbing_edit(payload, _activities(("2", "8a", "redpoint")))

    assert edited.activities[0].grade == Grade(
        original="8a", scale="french", french="8a", ordinal=30
    )
    assert edited.activities[0].count == 2
    assert edited.activities[0].style == "redpoint"


def test_a_blank_grade_or_style_means_none() -> None:
    edited = apply_climbing_edit(_PAYLOAD, _activities(("3", "  ", ""), ("2", "6B", "flash")))

    assert edited.activities[0].grade is None
    assert edited.activities[0].style is None
    assert edited.activities[1].style == "flash"


def test_what_the_edit_leaves_out_stays_as_it_was() -> None:
    payload: dict[str, JsonValue] = {
        "sport": "climbing",
        "activities": [
            {"kind": "lead", "count": 1, "attempts": 3, "route": {"name": "Hades"}},
            {"kind": "boulder", "count": 4},
        ],
    }

    edited = apply_climbing_edit(payload, {"activities": [{"count": "2"}]})

    assert edited.activities[0].kind == "lead"
    assert (edited.activities[0].count, edited.activities[0].attempts) == (2, 3)
    assert edited.activities[0].route is not None
    assert edited.activities[0].route.name == "Hades"
    assert (edited.activities[1].kind, edited.activities[1].count) == ("boulder", 4)


def test_no_activities_in_the_edit_changes_nothing() -> None:
    assert apply_climbing_edit(_PAYLOAD, {"rpe": "7"}) == ClimbingPayload.model_validate(_PAYLOAD)


@pytest.mark.parametrize(
    ("row", "field"),
    [
        (("0", "6A", ""), "count"),
        (("two", "6A", ""), "count"),
        (("", "6A", ""), "count"),
        (("1", "6A", "dyno"), "style"),
    ],
)
def test_the_payload_model_rejects_a_bad_value_at_its_field(
    row: tuple[str, str, str], field: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        apply_climbing_edit(_PAYLOAD, _activities(row, ("2", "6B", "")))

    assert [error["loc"] for error in caught.value.errors()] == [("activities", 0, field)]


def test_the_climbing_plugin_names_its_edit() -> None:
    spec = default_sport_registry().get("climbing").extraction

    assert spec is not None
    assert spec.apply_edit is apply_climbing_edit


def test_a_grade_that_isnt_text_is_rejected_at_the_grade() -> None:
    with pytest.raises(ValidationError) as caught:
        apply_climbing_edit(_PAYLOAD, {"activities": [{"grade": 6}]})

    assert [error["loc"] for error in caught.value.errors()] == [("activities", 0, "grade")]
