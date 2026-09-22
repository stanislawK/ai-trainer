from collections.abc import Callable
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.domain.llm_calls import LlmCallOutcome, NewLlmCall

_INSERT_USER = text("INSERT INTO users (id) VALUES (:id)")


async def _create_user(session_factory: Callable[[], AsyncSession]) -> UUID:
    user_id = uuid4()
    async with session_factory() as session:
        await session.execute(_INSERT_USER, {"id": user_id})
        await session.commit()
    return user_id


def _new_call(user_id: UUID, **overrides: object) -> NewLlmCall:
    defaults: dict[str, object] = {
        "user_id": user_id,
        "template_id": "greeter",
        "template_version": 1,
        "model": "openai/gpt-5-mini",
        "input_tokens": 10,
        "output_tokens": 5,
        "cost": Decimal("0.001200"),
        "latency_ms": 42,
        "outcome": LlmCallOutcome.SUCCESS,
    }
    defaults.update(overrides)
    return NewLlmCall(**defaults)  # type: ignore[arg-type]


async def test_record_persists_a_row_with_id_and_created_at(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _create_user(db_session_factory)
    repository = SqlAlchemyLlmCallsRepository(db_session_factory)

    recorded = await repository.record(_new_call(user_id))

    assert recorded.id is not None
    assert recorded.created_at is not None
    assert recorded.user_id == user_id
    assert recorded.cost == Decimal("0.001200")


async def test_list_for_user_is_tenant_scoped(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_a = await _create_user(db_session_factory)
    user_b = await _create_user(db_session_factory)
    repository = SqlAlchemyLlmCallsRepository(db_session_factory)

    await repository.record(_new_call(user_a, template_id="a-template"))
    await repository.record(_new_call(user_b, template_id="b-template"))

    calls_for_a = await repository.list_for_user(user_a)

    assert [call.template_id for call in calls_for_a] == ["a-template"]
    assert all(call.user_id == user_a for call in calls_for_a)


async def test_list_for_user_with_no_calls_is_empty(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _create_user(db_session_factory)
    repository = SqlAlchemyLlmCallsRepository(db_session_factory)

    assert await repository.list_for_user(user_id) == []
