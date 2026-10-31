"""Where each sport keeps a confirmed session's activities (ADR-0032). Climbing and cycling keep
them in the session's payload, through their plugin's payload model (ADR-0022); a sport with
its own tables has its own store, mapped by `sport_id` in the composition root."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from pydantic import BaseModel, JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import TrainingSessionOrm
from ai_trainer.application.ports.activities import ActivityStorePort, LoggedActivities
from ai_trainer.domain.sports.base import extend_payload, withdraw_payload
from ai_trainer.domain.sports.registry import SportRegistry

type ActivityStore = ActivityStorePort[AsyncSession]


class PayloadActivityStore:
    """Implements `ActivityStorePort` over `training_sessions.payload`, read and written only
    through the sport's payload model (ADR-0022, ticket #165)."""

    def __init__(self, payload_type: type[BaseModel]) -> None:
        self._payload_type = payload_type

    async def add(
        self, tx: AsyncSession, user_id: UUID, log: LoggedActivities
    ) -> dict[str, JsonValue]:
        if log.session_payload is None:
            return dict(log.addition)
        return extend_payload(self._payload_type, log.session_payload, log.addition).model_dump(
            mode="json"
        )

    async def withdraw(
        self, tx: AsyncSession, user_id: UUID, log: LoggedActivities
    ) -> dict[str, JsonValue]:
        assert log.session_payload is not None  # a session that keeps other logs has one
        return withdraw_payload(self._payload_type, log.session_payload, log.addition).model_dump(
            mode="json"
        )

    async def recent(
        self, tx: AsyncSession, user_id: UUID, session_ids: Sequence[UUID]
    ) -> list[Mapping[str, JsonValue]]:
        rows = await tx.execute(
            select(TrainingSessionOrm.id, TrainingSessionOrm.payload).where(
                TrainingSessionOrm.user_id == user_id, TrainingSessionOrm.id.in_(session_ids)
            )
        )
        payloads: dict[UUID, Mapping[str, JsonValue]] = dict(rows.tuples().all())
        return [payloads[session_id] for session_id in session_ids if session_id in payloads]


class ActivityStores:
    """The store of each sport: its own, or the payload store over its plugin's payload model."""

    def __init__(self, sports: SportRegistry, own: Mapping[str, ActivityStore]) -> None:
        self._stores: dict[str, ActivityStore] = {
            sport.id: PayloadActivityStore(sport.extraction.payload_type)
            for sport in sports.all()
            if sport.extraction is not None
        } | dict(own)

    def __call__(self, sport_id: str) -> ActivityStore:
        # Only a sport that can be logged has sessions, so every one has a store.
        return self._stores[sport_id]
