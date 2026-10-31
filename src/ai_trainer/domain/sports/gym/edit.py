"""The athlete's edit of a gym draft (PRD-0010 F2, B38, tickets #158, #159, #395 and #398): each
exercise's name, measure, each side, implements and RIR, and every set's reps, load in kg, time in
seconds, distance in metres and band, by position. What the form leaves out stays as it was, and a
blank field is cleared to None, never 0. A changed load is converted by code, as in an extraction;
an untouched one keeps what was typed. A set whose values are left as shown is unchanged, so an
edit of one set of a pyramid changes only that set."""

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, Field, JsonValue

from ai_trainer.domain.sports.gym.normalize import normalize_load
from ai_trainer.domain.sports.gym.payload import (
    MAX_DISTANCE_M,
    MAX_DURATION_S,
    MAX_IMPLEMENTS,
    MAX_RIR,
    MAX_SETS,
    BandLabel,
    Exercise,
    ExerciseName,
    GymPayload,
    GymSet,
    Load,
    Measure,
    measure_of,
)

_EXERCISE_FIELDS = ("name", "measure", "per_side", "implements", "rir")
_SET_FIELDS = ("reps", "load", "duration", "distance", "band")


class _SetForm(BaseModel):
    """One set's row, so a rejected value is located at `activities.<i>.sets.<j>.<field>` as the
    form names it. Time is in seconds and distance in metres."""

    reps: int | None = Field(default=None, ge=1)
    load: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    duration: int | None = Field(default=None, ge=0, le=MAX_DURATION_S)
    distance: float | None = Field(default=None, ge=0, le=MAX_DISTANCE_M, allow_inf_nan=False)
    band: BandLabel | None = None


class _ExerciseForm(BaseModel):
    """One exercise's row. A blank name is rejected; the other fields clear when blank."""

    name: ExerciseName = ""
    measure: Measure | None = None
    per_side: bool | None = None
    implements: int | None = Field(default=None, ge=1, le=MAX_IMPLEMENTS)
    rir: int | None = Field(default=None, ge=0, le=MAX_RIR)
    sets: list[_SetForm] | None = Field(default=None, max_length=MAX_SETS)


class _Form(BaseModel):
    activities: list[_ExerciseForm]


def apply_gym_edit(
    payload: Mapping[str, JsonValue], changes: Mapping[str, JsonValue]
) -> GymPayload:
    """`payload` is the stored gym payload and `changes` the athlete's form, with its
    `activities` as a list of `{name, measure, per_side, implements, rir, sets}` strings, where
    `sets` is a list of `{reps, load, duration, distance, band}` strings. Rows past the stored
    exercises are ignored. Raises `ValidationError` located at `activities.<i>.<field>` or
    `activities.<i>.sets.<j>.<field>` when a typed value is rejected."""
    current = GymPayload.model_validate(payload)
    edits = changes.get("activities")
    rows: list[dict[str, JsonValue]] = []
    if isinstance(edits, list):
        rows = [
            _row(edit, exercise) if isinstance(edit, dict) else {}
            for edit, exercise in zip(edits, current.activities, strict=False)
        ]
    form = _Form.model_validate({"activities": rows})
    edited = [
        _edited(exercise, typed)
        for exercise, typed in zip(current.activities, form.activities, strict=False)
    ]
    return GymPayload(activities=[*edited, *current.activities[len(edited) :]])


def _strip(edit: Mapping[str, JsonValue], fields: tuple[str, ...]) -> dict[str, JsonValue]:
    """The editable fields the row carries, stripped, with a blank one as none."""
    row: dict[str, JsonValue] = {}
    for field in fields:
        if field not in edit:
            continue
        value = edit[field]
        if isinstance(value, str):
            value = value.strip() or None
        row[field] = value
    return row


def _row(edit: Mapping[str, JsonValue], exercise: Exercise) -> dict[str, JsonValue]:
    row = _strip(edit, _EXERCISE_FIELDS)
    typed_sets = edit.get("sets")
    if isinstance(typed_sets, list):
        row["sets"] = [
            _set_row(typed, exercise.sets[index].load if index < len(exercise.sets) else None)
            if isinstance(typed, dict)
            else {}
            for index, typed in enumerate(typed_sets)
        ]
    return row


def _set_row(edit: Mapping[str, JsonValue], stored: Load | None) -> dict[str, JsonValue]:
    """A load that rounds to 0 kg is shown as "0", which a typed load can't be; left as shown,
    it is untouched, so the row drops it and what was typed stays."""
    row = _strip(edit, _SET_FIELDS)
    if stored is not None and stored.kg == 0 and _is_zero(row.get("load")):
        del row["load"]
    return row


def _is_zero(typed: JsonValue) -> bool:
    try:
        return float(str(typed)) == 0
    except ValueError:
        return False


def _edited(exercise: Exercise, form: _ExerciseForm) -> Exercise:
    fields = form.model_fields_set
    sets = exercise.sets
    if form.sets is not None:
        # Every removed set gone leaves one blank set: an exercise always has one.
        sets = [
            _edited_set(sets[index] if index < len(sets) else None, typed)
            for index, typed in enumerate(form.sets)
        ] or [GymSet()]
    # A settled exercise was settled under its name: another name is typed-only again (ticket
    # #397), so a confirm can't store the new name as an alias of the old exercise.
    renamed = "name" in fields and form.name != exercise.name_as_typed
    return exercise.model_copy(
        update={
            "name_as_typed": form.name if "name" in fields else exercise.name_as_typed,
            "exercise_id": None if renamed else exercise.exercise_id,
            "keep_name": False if renamed else exercise.keep_name,
            "measure": _measure(form.measure, sets),
            "per_side": bool(form.per_side) if "per_side" in fields else exercise.per_side,
            "implements": form.implements if "implements" in fields else exercise.implements,
            "rir": form.rir if "rir" in fields else exercise.rir,
            "sets": sets,
        }
    )


def _measure(picked: Measure | None, sets: Sequence[GymSet]) -> Measure:
    """Read in code from the first set that holds a value, as in an extraction, so it follows a
    load the athlete cleared. The athlete's pick counts only while every set is blank, since
    nothing yet says otherwise."""
    given = next((gym_set for gym_set in sets if gym_set != GymSet()), None)
    if given is None:
        return picked or measure_of(sets[0])
    return measure_of(given)


def _edited_set(stored: GymSet | None, form: _SetForm) -> GymSet:
    """The set as the form has it: a field the form doesn't carry stays as stored, a blank one
    is None."""
    fields = form.model_fields_set
    kept = stored or GymSet()
    return GymSet(
        reps=form.reps if "reps" in fields else kept.reps,
        load=_load(kept.load, form.load) if "load" in fields else kept.load,
        duration_s=form.duration if "duration" in fields else kept.duration_s,
        distance_m=form.distance if "distance" in fields else kept.distance_m,
        band_label=form.band if "band" in fields else kept.band_label,
    )


def _load(stored: Load | None, value: float | None) -> Load | None:
    """None when blank; the stored load when the athlete kept its kg, so what was typed stays;
    else the typed kg."""
    if value is None:
        return None
    if stored is not None and stored.kg == value:
        return stored
    return normalize_load(f"{value:g} kg", value, "kg")
