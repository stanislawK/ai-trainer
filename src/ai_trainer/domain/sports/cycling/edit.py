"""The athlete's edit of a cycling draft (PRD-0004 F2, tickets #158 and #160): each ride's
distance in km, moving time and elevation in metres, by position. What the edit leaves out stays
as it was, zones included, and `CyclingPayload` validates the result. A changed distance or
elevation is converted by code, as in an extraction; an untouched one keeps what was typed."""

from collections.abc import Mapping

from pydantic import BaseModel, Field, JsonValue

from ai_trainer.domain.sports.cycling.normalize import normalize_distance, normalize_elevation
from ai_trainer.domain.sports.cycling.payload import CyclingPayload

_EDITABLE = ("distance", "moving_time_min", "elevation")


class _RideForm(BaseModel):
    """One form row, so a rejected value is located at `activities.<i>.<field>` as the form
    names it."""

    distance: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    moving_time_min: int | None = Field(default=None, ge=1, le=1440)
    elevation: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class _Form(BaseModel):
    activities: list[_RideForm]


def apply_cycling_edit(
    payload: Mapping[str, JsonValue], changes: Mapping[str, JsonValue]
) -> CyclingPayload:
    """`payload` is the stored cycling payload and `changes` the athlete's form, with its
    `activities` as a list of `{distance, moving_time_min, elevation}` strings. Rows past the
    stored rides are ignored. Raises `ValidationError` located at `activities.<i>.<field>` when a
    typed value is rejected, or at the payload's own fields when the result isn't valid."""
    current = CyclingPayload.model_validate(payload)
    activities = [activity.model_dump(mode="json") for activity in current.activities]
    edits = changes.get("activities")
    rows: list[dict[str, JsonValue]] = []
    if isinstance(edits, list):
        rows = [_row(edit) if isinstance(edit, dict) else {} for edit in edits[: len(activities)]]
    form = _Form.model_validate({"activities": rows})
    for activity, row, ride in zip(activities, rows, form.activities, strict=False):
        _apply(activity, row, ride)
    return CyclingPayload.model_validate(
        {**current.model_dump(mode="json"), "activities": activities}
    )


def _row(edit: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The editable fields the row carries, stripped, with a blank one as none."""
    row: dict[str, JsonValue] = {}
    for field in _EDITABLE:
        if field not in edit:
            continue
        value = edit[field]
        if isinstance(value, str):
            value = value.strip() or None
        row[field] = value
    return row


def _apply(activity: dict[str, JsonValue], row: Mapping[str, JsonValue], ride: _RideForm) -> None:
    fields = ride.model_fields_set
    if "distance" in fields:
        activity["distance"] = _distance(activity.get("distance"), row["distance"], ride.distance)
    if "moving_time_min" in fields:
        activity["moving_time_min"] = ride.moving_time_min
    if "elevation" in fields:
        activity["elevation"] = _elevation(
            activity.get("elevation"), row["elevation"], ride.elevation
        )


def _distance(stored: JsonValue, typed: JsonValue, value: float | None) -> JsonValue:
    """None when blank; the stored distance when the athlete kept its km, so what was typed
    stays; else the typed km."""
    if value is None:
        return None
    if isinstance(stored, dict) and stored.get("km") == value:
        return stored
    return normalize_distance(f"{_text(typed)} km", value, "km").model_dump(mode="json")


def _elevation(stored: JsonValue, typed: JsonValue, value: float | None) -> JsonValue:
    """None when blank; the stored elevation when the athlete kept its metres, so what was typed
    stays; else the typed metres."""
    if value is None:
        return None
    if isinstance(stored, dict) and stored.get("m") == value:
        return stored
    return normalize_elevation(f"{_text(typed)} m", value, "m").model_dump(mode="json")


def _text(typed: JsonValue) -> str:
    return typed if isinstance(typed, str) else str(typed)
