from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import FeedbackEventOrm
from ai_trainer.domain.feedback import FeedbackEvent, FeedbackKind, FeedbackSignal, NewFeedbackEvent


def _to_domain(row: FeedbackEventOrm) -> FeedbackEvent:
    return FeedbackEvent(
        id=row.id,
        user_id=row.user_id,
        kind=FeedbackKind(row.kind),
        template_id=row.template_id,
        template_version=row.template_version,
        fields=tuple(row.fields),
        created_at=row.created_at,
    )


class SqlAlchemyFeedbackEventsRepository:
    """Implements `FeedbackEventsPort` (ADR-0034). `record` and `list_for_user` are scoped to a
    user; `signals` is the owner's report over all rows and selects neither user nor ID."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, event: NewFeedbackEvent) -> FeedbackEvent:
        async with self._session_factory() as session:
            row = FeedbackEventOrm(
                user_id=event.user_id,
                kind=event.kind.value,
                template_id=event.template_id,
                template_version=event.template_version,
                fields=list(event.fields),
                created_at=event.created_at,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def list_for_user(self, user_id: UUID) -> Sequence[FeedbackEvent]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(FeedbackEventOrm)
                .where(FeedbackEventOrm.user_id == user_id)
                .order_by(FeedbackEventOrm.created_at, FeedbackEventOrm.id)
            )
            return [_to_domain(row) for row in rows]

    async def signals(self, *, since: datetime | None = None) -> Sequence[FeedbackSignal]:
        query = select(
            FeedbackEventOrm.kind,
            FeedbackEventOrm.template_id,
            FeedbackEventOrm.template_version,
            FeedbackEventOrm.fields,
        )
        if since is not None:
            query = query.where(FeedbackEventOrm.created_at >= since)
        async with self._session_factory() as session:
            return [
                FeedbackSignal(
                    kind=FeedbackKind(kind),
                    template_id=template_id,
                    template_version=template_version,
                    fields=tuple(fields),
                )
                for kind, template_id, template_version, fields in await session.execute(query)
            ]
