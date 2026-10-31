"""The gym's `ActivityStore` (ADR-0032, ticket #376): a confirmed gym log's exercises go into
`gym_exercise_entries` and `gym_sets`, never into the session's payload, which is `{}` for a
session a gym log creates. Every statement filters by `user_id` (ADR-0022 invariant 6)."""

from collections.abc import Mapping, Sequence
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import GymExerciseEntryOrm, GymSetOrm
from ai_trainer.application.ports.activities import LoggedActivities
from ai_trainer.domain.sports.base import withdraw_payload
from ai_trainer.domain.sports.gym.payload import GymPayload
from ai_trainer.domain.sports.gym.rows import entries_of, unit_of


class GymActivityStore:
    """Implements `ActivityStorePort` over the gym's tables."""

    async def add(
        self, tx: AsyncSession, user_id: UUID, log: LoggedActivities
    ) -> dict[str, JsonValue]:
        """Appends the log's entries after the session's own; the caller holds the session's
        row lock, so two additions can't take the same position. A session confirmed before
        the tables keeps its v1 payload as it is (ADR-0022 invariant 4) until #377 backfills
        its rows."""
        last = await tx.scalar(
            select(func.coalesce(func.max(GymExerciseEntryOrm.position), 0)).where(
                GymExerciseEntryOrm.user_id == user_id,
                GymExerciseEntryOrm.session_id == log.session_id,
            )
        )
        entries: list[dict[str, object]] = []
        sets: list[dict[str, object]] = []
        for position, entry in enumerate(
            entries_of(GymPayload.model_validate(log.addition)), start=(last or 0) + 1
        ):
            entry_id = uuid4()
            entries.append(
                {
                    "id": entry_id,
                    "user_id": user_id,
                    "session_id": log.session_id,
                    "source_id": log.source_id,
                    "position": position,
                    "name_as_typed": entry.name_as_typed,
                    "measure": entry.measure,
                    "rir": entry.rir,
                }
            )
            sets.extend(
                {"user_id": user_id, "entry_id": entry_id, **row.model_dump()} for row in entry.sets
            )
        await tx.execute(insert(GymExerciseEntryOrm), entries)
        await tx.execute(insert(GymSetOrm), sets)
        return {} if log.session_payload is None else dict(log.session_payload)

    async def withdraw(
        self, tx: AsyncSession, user_id: UUID, log: LoggedActivities
    ) -> dict[str, JsonValue]:
        """Deletes the log's own entries, their sets with them. The source row's key would
        take them too; deleting them here keeps the store the one place that writes them.

        A log with no entries was confirmed before the tables: its exercises are taken out of
        the session's v1 payload instead, as before #376, so #377's backfill doesn't bring
        them back."""
        deleted = await tx.scalars(
            delete(GymExerciseEntryOrm)
            .where(
                GymExerciseEntryOrm.user_id == user_id,
                GymExerciseEntryOrm.source_id == log.source_id,
            )
            .returning(GymExerciseEntryOrm.id)
        )
        payload = {} if log.session_payload is None else dict(log.session_payload)
        if deleted.first() is not None or not payload:
            return payload
        try:
            return withdraw_payload(GymPayload, payload, log.addition).model_dump(mode="json")
        except ValidationError:
            # Nothing of the old payload is left: the session's other logs are all in rows.
            return {}

    async def recent(
        self, tx: AsyncSession, user_id: UUID, session_ids: Sequence[UUID]
    ) -> list[Mapping[str, JsonValue]]:
        """Each session's entries in order, with the unit of the first set's load, as
        `recent_exercises` reads them."""
        rows = await tx.execute(
            select(
                GymExerciseEntryOrm.session_id,
                GymExerciseEntryOrm.name_as_typed,
                GymSetOrm.load_original,
                GymSetOrm.load_kg,
            )
            .outerjoin(
                GymSetOrm,
                (GymSetOrm.entry_id == GymExerciseEntryOrm.id)
                & (GymSetOrm.user_id == GymExerciseEntryOrm.user_id)
                & (GymSetOrm.set_no == 1),
            )
            .where(
                GymExerciseEntryOrm.user_id == user_id,
                GymExerciseEntryOrm.session_id.in_(session_ids),
            )
            .order_by(GymExerciseEntryOrm.position)
        )
        activities: dict[UUID, list[JsonValue]] = {session_id: [] for session_id in session_ids}
        for session_id, name, load_original, load_kg in rows.tuples():
            unit = (
                None
                if load_original is None or load_kg is None
                else unit_of(load_original, load_kg)
            )
            activities[session_id].append({"name": name, "unit": unit})
        return [{"activities": exercises} for exercises in activities.values()]
