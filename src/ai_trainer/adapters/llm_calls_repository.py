from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import LlmCallOrm
from ai_trainer.domain.llm_calls import LlmCall, LlmCallOutcome, NewLlmCall


def _to_domain(row: LlmCallOrm) -> LlmCall:
    return LlmCall(
        id=row.id,
        user_id=row.user_id,
        template_id=row.template_id,
        template_version=row.template_version,
        model=row.model,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cost=row.cost,
        latency_ms=row.latency_ms,
        outcome=LlmCallOutcome(row.outcome),
        created_at=row.created_at,
    )


class SqlAlchemyLlmCallsRepository:
    """Implements `LlmCallsRepositoryPort` (ADR-0004: repositories live in adapters/)."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, call: NewLlmCall) -> LlmCall:
        async with self._session_factory() as session:
            row = LlmCallOrm(
                user_id=call.user_id,
                template_id=call.template_id,
                template_version=call.template_version,
                model=call.model,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cost=call.cost,
                latency_ms=call.latency_ms,
                outcome=call.outcome.value,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_domain(row)

    async def list_for_user(self, user_id: UUID) -> list[LlmCall]:
        async with self._session_factory() as session:
            rows = await session.scalars(select(LlmCallOrm).where(LlmCallOrm.user_id == user_id))
            return [_to_domain(row) for row in rows]
