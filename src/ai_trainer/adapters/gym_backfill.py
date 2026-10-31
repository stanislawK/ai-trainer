"""Backfill of gym sessions from before the typed tables (ADR-0032, ticket #377). It only
inserts: `training_sessions` is read, never written. A backfilled entry is the one with a NULL
`source_id`, which is what makes a second run find nothing. Every statement filters by
`user_id` (ADR-0022 invariant 6)."""

from collections.abc import Callable, Sequence
from uuid import uuid4

from sqlalchemy import exists, func, insert, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ai_trainer.adapters.orm import GymExerciseEntryOrm, GymSetOrm, TrainingSessionOrm
from ai_trainer.application.ports.gym_backfill import BackfillCandidate
from ai_trainer.domain.sports.gym.rows import GymEntryRow

_GYM = "gym"


def _backfilled() -> ColumnElement[bool]:
    return exists().where(
        GymExerciseEntryOrm.user_id == TrainingSessionOrm.user_id,
        GymExerciseEntryOrm.session_id == TrainingSessionOrm.id,
        GymExerciseEntryOrm.source_id.is_(None),
    )


def _has_activities() -> ColumnElement[bool]:
    """A session a gym log created after #376 stores `{}`; its rows are already the record."""
    return TrainingSessionOrm.payload != literal_column("'{}'::jsonb")


class SqlAlchemyGymBackfillStore:
    """Implements `GymBackfillPort`."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def candidates(self) -> list[BackfillCandidate]:
        async with self._session_factory() as db:
            rows = await db.execute(
                select(
                    TrainingSessionOrm.user_id,
                    TrainingSessionOrm.id,
                    TrainingSessionOrm.payload,
                    TrainingSessionOrm.payload_version,
                )
                .where(TrainingSessionOrm.sport_id == _GYM, _has_activities(), ~_backfilled())
                .order_by(TrainingSessionOrm.created_at, TrainingSessionOrm.id)
            )
            return [
                BackfillCandidate(
                    user_id=user_id, session_id=session_id, payload=payload, payload_version=version
                )
                for user_id, session_id, payload, version in rows.tuples()
            ]

    async def write(self, candidate: BackfillCandidate, entries: Sequence[GymEntryRow]) -> None:
        async with self._session_factory() as db, db.begin():
            # The row lock makes a concurrent run wait, then find the rows and write nothing. A
            # payload an undo rewrote since `candidates()` read it is left for the next run, so
            # an exercise the athlete took out is never written back from the stale copy.
            current = await db.scalar(
                select(TrainingSessionOrm.payload)
                .where(
                    TrainingSessionOrm.user_id == candidate.user_id,
                    TrainingSessionOrm.id == candidate.session_id,
                    ~_backfilled(),
                )
                .with_for_update()
            )
            if current is None or current != candidate.payload:
                return
            last = await db.scalar(
                select(func.coalesce(func.max(GymExerciseEntryOrm.position), 0)).where(
                    GymExerciseEntryOrm.user_id == candidate.user_id,
                    GymExerciseEntryOrm.session_id == candidate.session_id,
                )
            )
            entry_rows: list[dict[str, object]] = []
            set_rows: list[dict[str, object]] = []
            for position, entry in enumerate(entries, start=(last or 0) + 1):
                entry_id = uuid4()
                entry_rows.append(
                    {
                        "id": entry_id,
                        "user_id": candidate.user_id,
                        "session_id": candidate.session_id,
                        "source_id": None,
                        "position": position,
                        **entry.model_dump(exclude={"sets"}),
                    }
                )
                set_rows.extend(
                    {"user_id": candidate.user_id, "entry_id": entry_id, **row.model_dump()}
                    for row in entry.sets
                )
            await db.execute(insert(GymExerciseEntryOrm), entry_rows)
            await db.execute(insert(GymSetOrm), set_rows)
