from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.llm_calls_repository import (
    SqlAlchemyLlmCallsRepository,
    SqlAlchemyPictureDaySpend,
)
from ai_trainer.domain.llm_calls import LlmCallOutcome, NewLlmCall

_INSERT_USER = text("INSERT INTO users (id, sub, email) VALUES (:id, :sub, :email)")


async def _create_user(session_factory: Callable[[], AsyncSession]) -> UUID:
    user_id = uuid4()
    async with session_factory() as session:
        await session.execute(
            _INSERT_USER, {"id": user_id, "sub": str(user_id), "email": f"{user_id}@example.com"}
        )
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


# The card picture's daily cap (ADR-0031 *Picture on the card*, #643). A day far in the past keeps
# rows other tests commit today out of the sum.
_DAY = datetime(2001, 2, 3, tzinfo=UTC)
_PRICE = Decimal("0.0336")


async def _call_at(
    factory: Callable[[], AsyncSession],
    user_id: UUID,
    template_id: str,
    cost: Decimal | None,
    at: datetime,
) -> None:
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO llm_calls (id, user_id, template_id, template_version, model, "
                "input_tokens, output_tokens, cost, latency_ms, outcome, created_at) VALUES "
                "(:id, :user_id, :template_id, 1, 'm', 0, 0, :cost, 1, 'success', :at)"
            ),
            {"id": uuid4(), "user_id": user_id, "template_id": template_id, "cost": cost, "at": at},
        )
        await session.commit()


async def test_card_picture_spend_sums_today_path_rows_null_picture_cost_at_price(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _create_user(db_session_factory)
    admin = await _create_user(db_session_factory)
    noon = _DAY + timedelta(hours=12)
    for user_id, template_id, cost, at in (
        (athlete, "media.picture_brief", Decimal("0.001"), noon),
        (athlete, "picture_brief_check", Decimal("0.002"), noon),
        (athlete, "picture_brief_check", None, noon),
        (athlete, "media.picture", Decimal("0.04"), noon),
        # A picture with no cost counts at its price, whoever wrote it.
        (admin, "media.picture", None, _DAY),
        # Left out: another template, yesterday, and tomorrow's first instant.
        (athlete, "gym.answer", Decimal(5), noon),
        (athlete, "media.picture", Decimal(5), _DAY - timedelta(microseconds=1)),
        (athlete, "media.picture", Decimal(5), _DAY + timedelta(days=1)),
    ):
        await _call_at(db_session_factory, user_id, template_id, cost, at)
    spend = SqlAlchemyPictureDaySpend(
        db_session_factory,
        picture_template="media.picture",
        other_templates=("media.picture_brief", "picture_brief_check"),
    )

    spent = await spend.spent_between(_DAY, _DAY + timedelta(days=1), picture_price=_PRICE)

    assert spent == Decimal("0.001") + Decimal("0.002") + Decimal("0.04") + _PRICE


async def test_card_picture_spend_is_zero_on_a_day_without_rows(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    spend = SqlAlchemyPictureDaySpend(
        db_session_factory, picture_template="media.picture", other_templates=()
    )
    start = datetime(2000, 1, 1, tzinfo=UTC)

    assert await spend.spent_between(start, start + timedelta(days=1), picture_price=_PRICE) == 0
