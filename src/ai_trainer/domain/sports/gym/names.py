"""The names on a gym draft, as settling them reads and changes them (B37, ADR-0031, ticket #397).
The entries of one name share one answer: a pyramid is three entries until #398, and "Bench
Press" and "bench  press" are one question. This only groups a draft's own entries, so it needs
no `unaccent`; matching a name against stored aliases is the database's (`exercise_name_norm`)."""

from collections.abc import Collection, Mapping
from uuid import UUID

from ai_trainer.domain.clarification import KEEP_NAME_OPTION
from ai_trainer.domain.exercises import MAX_EXERCISE_NAME
from ai_trainer.domain.sports.gym.payload import Exercise, GymPayload

GYM_SPORT_ID = "gym"


def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


def unsettled(payload: GymPayload, *, settled: Collection[str] = ()) -> tuple[Exercise, ...]:
    """The first entry of each name still open, in draft order, leaving out `settled`. A name too
    long to store as an alias stays typed-only, so it is never asked about."""
    done = {_key(name) for name in settled}
    found: dict[str, Exercise] = {}
    for exercise in payload.activities:
        key = _key(exercise.name_as_typed)
        if (
            exercise.exercise_id is None
            and not exercise.keep_name
            and len(exercise.name_as_typed) <= MAX_EXERCISE_NAME
            and key not in done
        ):
            found.setdefault(key, exercise)
    return tuple(found.values())


def spellings_of(payload: GymPayload, exercises: Collection[Exercise]) -> tuple[str, ...]:
    """Every way the draft types the names of `exercises`, each once, in draft order."""
    keys = {_key(exercise.name_as_typed) for exercise in exercises}
    return tuple(
        dict.fromkeys(
            exercise.name_as_typed
            for exercise in payload.activities
            if _key(exercise.name_as_typed) in keys
        )
    )


def with_exercises(payload: GymPayload, exercise_ids: Mapping[str, UUID]) -> GymPayload:
    """The payload with each entry named in `exercise_ids`, by its name as typed, settled."""
    return payload.model_copy(
        update={
            "activities": [
                exercise.model_copy(update={"exercise_id": exercise_ids[exercise.name_as_typed]})
                if exercise.name_as_typed in exercise_ids
                else exercise
                for exercise in payload.activities
            ]
        }
    )


def answered(payload: GymPayload, subject: str, option_id: str) -> GymPayload:
    """The payload as the athlete's tap reads it: "Keep my name", or the candidate exercise whose
    ID the option carries, on every entry of `subject`."""
    change: dict[str, object] = (
        {"keep_name": True} if option_id == KEEP_NAME_OPTION else {"exercise_id": UUID(option_id)}
    )
    key = _key(subject)
    return payload.model_copy(
        update={
            "activities": [
                exercise.model_copy(update=change)
                if _key(exercise.name_as_typed) == key
                else exercise
                for exercise in payload.activities
            ]
        }
    )


def keys_of(payload: GymPayload) -> frozenset[str]:
    """Each name on the draft once, as the draft groups them."""
    return frozenset(_key(exercise.name_as_typed) for exercise in payload.activities)


def mentions(payload: GymPayload, subject: str) -> bool:
    """Whether an entry of the draft still carries the name."""
    key = _key(subject)
    return any(_key(exercise.name_as_typed) == key for exercise in payload.activities)
