"""The athlete's edit of a gym draft (PRD-0004 F2, tickets #158, #159 and #395): each exercise's
name, number of sets, reps, load in kg and RIR, by position. What the edit leaves out stays as
it was. A changed load is converted by code, as in an extraction; an untouched one keeps what was
typed. The form shows the first set's reps and load until #398 edits set by set: a value left as
shown touches no set, so a time, a band or a set that differs stays, and a changed one goes on
every set."""

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, Field, JsonValue

from ai_trainer.domain.sports.gym.normalize import normalize_load
from ai_trainer.domain.sports.gym.payload import (
    MAX_RIR,
    MAX_SETS,
    Exercise,
    ExerciseName,
    GymPayload,
    GymSet,
    Load,
    measure_of,
)

_EDITABLE = ("name", "sets", "reps", "load", "rir")


class _ExerciseForm(BaseModel):
    """One form row, so a rejected value is located at `activities.<i>.<field>` as the form
    names it. A blank name is rejected; the other fields clear when blank."""

    name: ExerciseName = ""
    sets: int | None = Field(default=None, ge=1, le=MAX_SETS)
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
    typed value is rejected."""
    current = GymPayload.model_validate(payload)
    edits = changes.get("activities")
    rows: list[dict[str, JsonValue]] = []
    if isinstance(edits, list):
        rows = [
            _row(edit) if isinstance(edit, dict) else {}
            for edit in edits[: len(current.activities)]
        ]
    for exercise, row in zip(current.activities, rows, strict=False):
        _keep_tiny_load(exercise.sets[0].load, row)
    form = _Form.model_validate({"activities": rows})
    edited = [
        _edited(exercise, row, typed)
        for exercise, row, typed in zip(current.activities, rows, form.activities, strict=False)
    ]
    return GymPayload(activities=[*edited, *current.activities[len(edited) :]])


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


def _keep_tiny_load(stored: Load | None, row: dict[str, JsonValue]) -> None:
    """A load that rounds to 0 kg is shown as "0", which a typed load can't be; left as shown,
    it is untouched, so the row drops it and what was typed stays."""
    if stored is not None and stored.kg == 0 and _is_zero(row.get("load")):
        del row["load"]


def _is_zero(typed: JsonValue) -> bool:
    try:
        return float(str(typed)) == 0
    except ValueError:
        return False


def _edited(exercise: Exercise, row: Mapping[str, JsonValue], form: _ExerciseForm) -> Exercise:
    fields = form.model_fields_set
    sets = _resized(exercise.sets, form.sets or 1) if "sets" in fields else exercise.sets
    first = sets[0]
    if "reps" in fields and form.reps != first.reps:
        sets = [gym_set.model_copy(update={"reps": form.reps}) for gym_set in sets]
    if "load" in fields:
        load = _load(first.load, row["load"], form.load)
        if load != first.load:
            sets = [gym_set.model_copy(update={"load": load}) for gym_set in sets]
    # A settled exercise was settled under its name: another name is typed-only again (ticket
    # #397), so a confirm can't store the new name as an alias of the old exercise.
    renamed = "name" in fields and form.name != exercise.name_as_typed
    return exercise.model_copy(
        update={
            "name_as_typed": form.name if "name" in fields else exercise.name_as_typed,
            "exercise_id": None if renamed else exercise.exercise_id,
            "keep_name": False if renamed else exercise.keep_name,
            "rir": form.rir if "rir" in fields else exercise.rir,
            "measure": measure_of(sets[0]),
            "sets": sets,
        }
    )


def _resized(sets: Sequence[GymSet], count: int) -> list[GymSet]:
    """Cut from the end, or grown by copies of the last set. A blank count is one set."""
    return [*sets[:count], *[sets[-1]] * (count - len(sets))]


def _load(stored: Load | None, typed: JsonValue, value: float | None) -> Load | None:
    """None when blank; the stored load when the athlete kept its kg, so what was typed stays;
    else the typed kg."""
    if value is None:
        return None
    if stored is not None and stored.kg == value:
        return stored
    text = typed if isinstance(typed, str) else str(typed)
    return normalize_load(f"{text} kg", value, "kg")
