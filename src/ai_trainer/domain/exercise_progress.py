"""One entry's number on a progress chart (PRD 0011 M2g, B38, B39, ticket #517): the heaviest
set's kg, the most reps, the longest hold or carry, or the place of the heaviest ranked band in
the athlete's own order. A set without the value is skipped, never estimated (B38), and a band's
place comes only from the athlete's order, never from its colour (B39)."""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from ai_trainer.domain.exercises import ExerciseEntry, ExerciseMeasure, LoggedSet


class EntryBest(BaseModel):
    """What an entry plots and says. `value` is `None` for a band with no place in the athlete's
    order: the chart marks the day with a dashed line and plots nothing."""

    model_config = ConfigDict(frozen=True)

    value: float | None
    text: str


def _number(value: float) -> str:
    return f"{round(value, 1):g}"


def _load_reps(sets: Sequence[LoggedSet]) -> EntryBest | None:
    loaded = [s for s in sets if s.load_kg is not None]
    if not loaded:
        return None
    best = max(loaded, key=lambda s: (s.load_kg or 0.0, s.reps or 0))
    kg = f"{_number(best.load_kg or 0.0)} kg"
    text = f"{best.reps} @ {kg}" if best.reps else kg
    return EntryBest(value=best.load_kg, text=text)


def _reps(sets: Sequence[LoggedSet]) -> EntryBest | None:
    counted = [s.reps for s in sets if s.reps is not None]
    if not counted:
        return None
    most = max(counted)
    return EntryBest(value=float(most), text=f"{most} {'rep' if most == 1 else 'reps'}")


def _time(sets: Sequence[LoggedSet]) -> EntryBest | None:
    held = [s.duration_s for s in sets if s.duration_s is not None]
    if not held:
        return None
    longest = max(held)
    return EntryBest(value=float(longest), text=f"{longest} s")


def _distance(sets: Sequence[LoggedSet]) -> EntryBest | None:
    carried = [s.distance_m for s in sets if s.distance_m is not None]
    if not carried:
        return None
    longest = max(carried)
    return EntryBest(value=longest, text=f"{_number(longest)} m")


def _band(sets: Sequence[LoggedSet], band_order: Sequence[str]) -> EntryBest | None:
    labels = [s.band_label for s in sets if s.band_label is not None]
    if not labels:
        return None
    places = {label: place for place, label in enumerate(band_order, start=1)}
    ranked = [label for label in labels if label in places]
    if not ranked:
        return EntryBest(value=None, text=f"{labels[0]} · not in your order")
    heaviest = max(ranked, key=lambda label: places[label])
    return EntryBest(value=float(places[heaviest]), text=f"{heaviest} band")


def _fallback(sets: Sequence[LoggedSet]) -> EntryBest | None:
    """What an entry says when its measure's own value is missing: the first value any set
    carries, never plotted and never estimated (B38)."""
    for pick in (_reps, _load_reps, _time, _distance):
        found = pick(sets)
        if found is not None:
            return EntryBest(value=None, text=found.text)
    labels = [s.band_label for s in sets if s.band_label is not None]
    return EntryBest(value=None, text=f"{labels[0]} band") if labels else None


def best_of(entry: ExerciseEntry, band_order: Sequence[str]) -> EntryBest | None:
    """The entry's best set by its measure. With no set carrying that measure, the first other
    value a set has, as text with no plotted value; `None` when no set has any value."""
    found: EntryBest | None
    match entry.measure:
        case ExerciseMeasure.LOAD_REPS:
            found = _load_reps(entry.sets)
        case ExerciseMeasure.REPS:
            found = _reps(entry.sets)
        case ExerciseMeasure.TIME:
            found = _time(entry.sets)
        case ExerciseMeasure.DISTANCE:
            found = _distance(entry.sets)
        case ExerciseMeasure.BAND_REPS:
            found = _band(entry.sets, band_order)
    return found if found is not None else _fallback(entry.sets)
