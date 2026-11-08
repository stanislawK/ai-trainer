"""The athlete's edit of a saved session (PRD 0011 B4, ADR-0022, ADR-0032, ticket #516): one
transaction locks the session's row, refuses the edit if its `updated_at` is not the one the
form was rendered with, has the sport's `ActivityStore` replace its activities, and
writes the payload it keeps and `updated_at` from the clock. Every statement filters by
`user_id` (ADR-0022 invariant 6)."""

from collections.abc import Callable, Mapping
from datetime import datetime
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.activity_stores import ActivityStore
from ai_trainer.adapters.orm import TrainingSessionOrm
from ai_trainer.application.ports.activities import EditedActivities
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.domain.history import SessionChangedError
from ai_trainer.domain.training import SessionNotFoundError


class SqlAlchemySessionEditor:
    """Implements `SessionEditorPort`."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        stores: Callable[[str], ActivityStore],
        clock: ClockPort,
    ) -> None:
        self._session_factory = session_factory
        self._stores = stores
        self._clock = clock

    async def replace_activities(
        self,
        user_id: UUID,
        session_id: UUID,
        *,
        sport_id: str,
        payload: Mapping[str, JsonValue],
        expected_updated_at: datetime,
    ) -> None:
        async with self._session_factory() as db:
            training = await db.scalar(
                select(TrainingSessionOrm)
                .where(
                    TrainingSessionOrm.id == session_id,
                    TrainingSessionOrm.user_id == user_id,
                    TrainingSessionOrm.sport_id == sport_id,
                )
                .with_for_update()
            )
            if training is None:
                raise SessionNotFoundError(session_id)
            if training.updated_at != expected_updated_at:
                raise SessionChangedError(session_id)
            training.payload = await self._stores(sport_id).replace(
                db,
                user_id,
                EditedActivities(
                    session_id=session_id, session_payload=training.payload, payload=payload
                ),
            )
            training.updated_at = self._clock.now()
            await db.commit()
