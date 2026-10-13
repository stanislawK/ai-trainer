"""The athlete's edit of a climbing draft (PRD-0004 F2, ticket #158): each activity's count,
grade and style, by position. What the edit leaves out stays as it was, and `ClimbingPayload`
validates the result. A changed grade's scale comes from code, as in an extraction."""

from collections.abc import Mapping

from pydantic import JsonValue

from ai_trainer.domain.sports.climbing.normalize import normalize_grade
from ai_trainer.domain.sports.climbing.payload import ClimbingPayload

_EDITABLE = ("count", "grade", "style")


def apply_climbing_edit(
    payload: Mapping[str, JsonValue], changes: Mapping[str, JsonValue]
) -> ClimbingPayload:
    """`payload` is the stored climbing payload and `changes` the athlete's form, with its
    `activities` as a list of `{count, grade, style}` strings. Raises `ValidationError`, located
    at the payload's own fields, when the result isn't a valid payload."""
    current = ClimbingPayload.model_validate(payload)
    activities = [activity.model_dump(mode="json") for activity in current.activities]
    edits = changes.get("activities")
    if isinstance(edits, list):
        for activity, edit in zip(activities, edits, strict=False):
            if isinstance(edit, dict):
                _apply(activity, edit)
    return ClimbingPayload.model_validate(
        {**current.model_dump(mode="json"), "activities": activities}
    )


def _apply(activity: dict[str, JsonValue], edit: Mapping[str, JsonValue]) -> None:
    for field in _EDITABLE:
        if field not in edit:
            continue
        value = edit[field]
        text = value.strip() if isinstance(value, str) else value
        if field == "grade":
            activity["grade"] = _grade(activity.get("grade"), text)
        elif field == "style":
            activity["style"] = text or None
        else:
            activity[field] = text


def _grade(stored: JsonValue, typed: JsonValue) -> JsonValue:
    """The stored grade when the athlete kept its text, so its derived fields stay; none when
    blank; else the typed text with its scale detected."""
    if not typed:
        return None
    if isinstance(stored, dict) and stored.get("original") == typed:
        return stored
    if not isinstance(typed, str):
        return typed
    return normalize_grade(typed).model_dump(mode="json")
