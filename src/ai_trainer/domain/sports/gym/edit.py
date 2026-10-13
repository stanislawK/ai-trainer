"""The athlete's edit of a gym draft (PRD-0004 F2, tickets #158 and #159): each exercise's
name, sets, reps, load in kg and RIR, by position. What the edit leaves out stays as it was, and
`GymPayload` validates the result. A changed load is converted by code, as in an extraction; an
untouched one keeps what was typed."""

from collections.abc import Mapping

from pydantic import BaseModel, Field, JsonValue

from ai_trainer.domain.sports.gym.normalize import normalize_load
from ai_trainer.domain.sports.gym.payload import MAX_RIR, ExerciseName, GymPayload

_EDITABLE = ("name", "sets", "reps", "load", "rir")


class _ExerciseForm(BaseModel):
    """One form row, so a rejected value is located at `activities.<i>.<field>` as the form
    names it. A blank name is rejected; the other fields clear when blank."""

    name: ExerciseName = ""
    sets: int | None = Field(default=None, ge=1)
    reps: int | None = Field(default=None, ge=1)
    load: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    rir: int | None = Field(default=None, ge=0, le=MAX_RIR)


class _Form(BaseModel):
    activities: list[_ExerciseForm]


def apply_gym_edit(
    payload: Mapping[str, JsonValue], changes: Mapping[str, JsonValue]
) -> GymPayload:
    """`payload` is the stored gym payload and `changes` the athlete's form, with its
    `activities` as a list of `{name, sets, reps, load, rir}` strings. Rows past the stored
    exercises are ignored. Raises `ValidationError` located at `activities.<i>.<field>` when a
    typed value is rejected, or at the payload's own fields when the result isn't valid."""
    current = GymPayload.model_validate(payload)
    activities = [exercise.model_dump(mode="json") for exercise in current.activities]
    edits = changes.get("activities")
    rows: list[dict[str, JsonValue]] = []
    if isinstance(edits, list):
        rows = [_row(edit) if isinstance(edit, dict) else {} for edit in edits[: len(activities)]]
    for activity, row in zip(activities, rows, strict=False):
        _keep_tiny_load(activity, row)
    form = _Form.model_validate({"activities": rows})
    for activity, row, exercise in zip(activities, rows, form.activities, strict=False):
        _apply(activity, row, exercise)
    return GymPayload.model_validate({**current.model_dump(mode="json"), "activities": activities})


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


def _keep_tiny_load(activity: Mapping[str, JsonValue], row: dict[str, JsonValue]) -> None:
    """A load that rounds to 0 kg is shown as "0", which a typed load can't be; left as shown,
    it is untouched, so the row drops it and what was typed stays."""
    stored = activity.get("load")
    if isinstance(stored, dict) and stored.get("kg") == 0 and _is_zero(row.get("load")):
        del row["load"]


def _is_zero(typed: JsonValue) -> bool:
    try:
        return float(str(typed)) == 0
    except ValueError:
        return False


def _apply(
    activity: dict[str, JsonValue], row: Mapping[str, JsonValue], exercise: _ExerciseForm
) -> None:
    fields = exercise.model_fields_set
    if "name" in fields:
        activity["name"] = exercise.name
    for field in ("sets", "reps", "rir"):
        if field in fields:
            activity[field] = getattr(exercise, field)
    if "load" in fields:
        activity["load"] = _load(activity.get("load"), row["load"], exercise.load)


def _load(stored: JsonValue, typed: JsonValue, value: float | None) -> JsonValue:
    """None when blank; the stored load when the athlete kept its kg, so what was typed stays;
    else the typed kg."""
    if value is None:
        return None
    if isinstance(stored, dict) and stored.get("kg") == value:
        return stored
    text = typed if isinstance(typed, str) else str(typed)
    return normalize_load(f"{text} kg", value, "kg").model_dump(mode="json")
