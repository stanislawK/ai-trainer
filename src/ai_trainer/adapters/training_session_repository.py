from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import Row, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.activity_stores import ActivityStore
from ai_trainer.adapters.orm import TrainingSessionOrm, TrainingSessionSourceOrm
from ai_trainer.domain.training import SessionSpan, SessionSummary

_LAST_LOGGED_AT = func.max(TrainingSessionSourceOrm.logged_at)


def select_spans(user_id: UUID) -> Select[tuple[Any, ...]]:
    """The user's sessions as `SessionSpan` rows, one per session with at least one source.
    The sources are joined on their `user_id` too, as every query here filters by it (ADR-0022
    invariant 6)."""
    return (
        select(
            TrainingSessionOrm.id,
            TrainingSessionOrm.occurred_on,
            func.min(TrainingSessionSourceOrm.logged_at),
            _LAST_LOGGED_AT,
        )
        .join(
            TrainingSessionSourceOrm,
            (TrainingSessionSourceOrm.session_id == TrainingSessionOrm.id)
            & (TrainingSessionSourceOrm.user_id == TrainingSessionOrm.user_id),
        )
        .where(TrainingSessionOrm.user_id == user_id)
        .group_by(TrainingSessionOrm.id)
    )


def to_span(row: Row[tuple[Any, ...]]) -> SessionSpan:
    session_id, occurred_on, first_logged_at, last_logged_at = row
    return SessionSpan(
        id=session_id,
        occurred_on=occurred_on,
        first_logged_at=first_logged_at,
        last_logged_at=last_logged_at,
    )


async def spans_of(
    session: AsyncSession, user_id: UUID, session_ids: Sequence[UUID]
) -> dict[UUID, SessionSpan]:
    """The user's sessions among `session_ids`, by ID; another user's or a removed one is
    left out."""
    if not session_ids:
        return {}
    rows = await session.execute(
        select_spans(user_id).where(TrainingSessionOrm.id.in_(session_ids))
    )
    return {span.id: span for span in map(to_span, rows.all())}


class SqlAlchemyTrainingSessionRepository:
    """Implements `TrainingSessionRepositoryPort` and `SessionHistoryPort` (ADR-0004,
    ADR-0022). A session's activities are read through its sport's `ActivityStore`
    (ADR-0032)."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        stores: Callable[[str], ActivityStore],
    ) -> None:
        self._session_factory = session_factory
        self._stores = stores

    async def latest_in_sport(self, user_id: UUID, sport_id: str) -> SessionSpan | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select_spans(user_id)
                    .where(TrainingSessionOrm.sport_id == sport_id)
                    .order_by(_LAST_LOGGED_AT.desc(), TrainingSessionOrm.id)
                    .limit(1)
                )
            ).one_or_none()
        return None if row is None else to_span(row)

    async def recent_payloads(
        self, user_id: UUID, sport_id: str, limit: int
    ) -> Sequence[Mapping[str, JsonValue]]:
        async with self._session_factory() as session:
            session_ids = await session.scalars(
                select(TrainingSessionOrm.id)
                .where(
                    TrainingSessionOrm.user_id == user_id,
                    TrainingSessionOrm.sport_id == sport_id,
                )
                .order_by(TrainingSessionOrm.occurred_on.desc(), TrainingSessionOrm.id)
                .limit(limit)
            )
            return await self._stores(sport_id).recent(session, user_id, list(session_ids.all()))

    async def list_sessions(
        self,
        user_id: UUID,
        *,
        sport_id: str | None,
        since: date | None,
        until: date | None,
        limit: int,
    ) -> Sequence[SessionSummary]:
        query = select(
            TrainingSessionOrm.id,
            TrainingSessionOrm.sport_id,
            TrainingSessionOrm.occurred_on,
            TrainingSessionOrm.duration_min,
            TrainingSessionOrm.rpe,
            TrainingSessionOrm.feeling,
            TrainingSessionOrm.notes,
        ).where(TrainingSessionOrm.user_id == user_id)
        if sport_id is not None:
            query = query.where(TrainingSessionOrm.sport_id == sport_id)
        if since is not None:
            query = query.where(TrainingSessionOrm.occurred_on >= since)
        if until is not None:
            query = query.where(TrainingSessionOrm.occurred_on <= until)
        async with self._session_factory() as session:
            rows = await session.execute(
                query.order_by(
                    TrainingSessionOrm.occurred_on.desc(), TrainingSessionOrm.created_at.desc()
                ).limit(limit)
            )
            return [SessionSummary.model_validate(row, from_attributes=True) for row in rows]
