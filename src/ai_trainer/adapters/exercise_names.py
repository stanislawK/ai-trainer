"""What a gym confirm settles about its names (B37, ADR-0031, ticket #397), in the confirm's own
transaction: the custom exercise for a kept name, the alias for a settled one, and the athlete's
earlier typed-only entries of that name. So a discarded draft leaves nothing behind. Every
statement filters by `user_id`."""

from collections.abc import Collection, Mapping
from uuid import UUID

from sqlalchemy import ARRAY, Text, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_repository import custom_row, visible_to, write_user_alias
from ai_trainer.adapters.orm import ExerciseAliasOrm, ExerciseOrm, GymExerciseEntryOrm
from ai_trainer.domain.exercises import MAX_EXERCISE_NAME
from ai_trainer.domain.sports.gym.names import GYM_SPORT_ID
from ai_trainer.domain.sports.gym.payload import Exercise, GymPayload


async def _norms(tx: AsyncSession, names: Collection[str]) -> Mapping[str, str]:
    """Each name as the database normalizes an alias, so a lookup can't differ from a store."""
    typed = func.unnest(cast(list(names), ARRAY(Text))).column_valued("typed")
    rows = await tx.execute(select(typed, func.exercise_name_norm(typed)))
    return dict(rows.tuples().all())


async def _known_aliases(
    tx: AsyncSession, user_id: UUID, norms: Collection[str]
) -> dict[str, UUID]:
    """The athlete's gym aliases among `norms`: one of another sport's exercises never names a
    gym entry, as `match_user_aliases` reads it too."""
    rows = await tx.execute(
        select(ExerciseAliasOrm.alias_norm, ExerciseAliasOrm.exercise_id)
        .join(ExerciseOrm, ExerciseOrm.id == ExerciseAliasOrm.exercise_id)
        .where(
            ExerciseAliasOrm.user_id == user_id,
            ExerciseAliasOrm.alias_norm.in_(norms),
            ExerciseOrm.sport_id == GYM_SPORT_ID,
        )
    )
    return dict(rows.tuples().all())


async def _visible(
    tx: AsyncSession, user_id: UUID, exercises: Collection[Exercise]
) -> frozenset[UUID]:
    """The draft's exercises the athlete can see: an ID from anywhere else settles nothing."""
    claimed = {exercise.exercise_id for exercise in exercises} - {None}
    if not claimed:
        return frozenset()
    rows = await tx.scalars(
        select(ExerciseOrm.id).where(ExerciseOrm.id.in_(claimed), visible_to(user_id))
    )
    return frozenset(rows)


async def _link_earlier(tx: AsyncSession, user_id: UUID, norm: str, exercise_id: UUID) -> None:
    await tx.execute(
        update(GymExerciseEntryOrm)
        .where(
            GymExerciseEntryOrm.user_id == user_id,
            GymExerciseEntryOrm.exercise_id.is_(None),
            func.exercise_name_norm(GymExerciseEntryOrm.name_as_typed) == norm,
        )
        .values(exercise_id=exercise_id)
    )


async def settle_names(tx: AsyncSession, user_id: UUID, payload: GymPayload) -> Mapping[str, UUID]:
    """The exercise of each name the log settles or the athlete settled before, by its name as
    typed. A name left out stays typed-only: too long to be an alias, or not settled at all."""
    by_name = {
        exercise.name_as_typed: exercise
        for exercise in payload.activities
        if len(exercise.name_as_typed) <= MAX_EXERCISE_NAME
    }
    if not by_name:
        return {}
    if any(exercise.keep_name for exercise in by_name.values()):
        # Two confirms at once make one custom exercise: the second waits, then finds the alias.
        await tx.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(str(user_id), 0))))
    norms = await _norms(tx, by_name)
    known = await _known_aliases(tx, user_id, set(norms.values()))
    visible = await _visible(tx, user_id, by_name.values())
    settled: dict[str, UUID] = {}
    for name, exercise in by_name.items():
        norm = norms[name]
        claimed = exercise.exercise_id
        if claimed is not None and claimed in visible:
            exercise_id: UUID | None = claimed
        elif exercise.keep_name:
            exercise_id = known.get(norm) or await _new_custom(tx, user_id, exercise)
        else:
            # Typed-only on the card, as after a rename: a name already settled still links.
            exercise_id = known.get(norm)
            if exercise_id is not None:
                settled[name] = exercise_id
            continue
        assert exercise_id is not None  # both branches above name one
        settled[name] = exercise_id
        if known.get(norm) == exercise_id:
            # Settled by this very alias, whose confirm linked the earlier entries already: the
            # routine log of a known name writes nothing more.
            continue
        known[norm] = exercise_id
        await write_user_alias(tx, user_id, exercise_id, name)
        await _link_earlier(tx, user_id, norm, exercise_id)
    return settled


async def _new_custom(tx: AsyncSession, user_id: UUID, exercise: Exercise) -> UUID:
    row = custom_row(
        user_id, sport_id=GYM_SPORT_ID, name=exercise.name_as_typed, measure=exercise.measure
    )
    tx.add(row)
    await tx.flush()
    return row.id
