"""The exercise search's embedding call is charged to the athlete's model-call budget (ADR-0029,
ticket #429). A log with N new names makes N calls at once on the log path, which holds no slot of
its own, so they count like any text or decision call. A refused call is not sent."""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

from ai_trainer.application.budgeted_gateways import BudgetedQueryEmbeddings
from ai_trainer.application.model_budget import ModelCallBudget
from ai_trainer.domain.embeddings import Embedded, EmbeddingOutcome, QueryTask, RateLimited
from tests.ingestion_support import FrozenClock

_USER = uuid4()


class _Embeddings:
    """Records every query it is sent, and holds each one open for `delay` seconds."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.sent: list[tuple[str, UUID, QueryTask]] = []

    async def embed_query(
        self, text: str, *, acting_user_id: UUID, task: QueryTask = QueryTask.QUESTION
    ) -> EmbeddingOutcome:
        self.sent.append((text, acting_user_id, task))
        await asyncio.sleep(self.delay)
        return Embedded(vectors=((0.0,),), model="m")


def _budget(clock: FrozenClock, *, per_hour: int, in_flight: int = 5) -> ModelCallBudget:
    return ModelCallBudget(clock, per_hour=per_hour, in_flight=in_flight)


async def test_an_allowed_query_reaches_the_gateway_with_its_task_and_athlete() -> None:
    inner = _Embeddings()
    wrapped = BudgetedQueryEmbeddings(inner, _budget(FrozenClock(), per_hour=2))

    outcome = await wrapped.embed_query("face pull", acting_user_id=_USER, task=QueryTask.EXERCISE)

    assert outcome == Embedded(vectors=((0.0,),), model="m")
    assert inner.sent == [("face pull", _USER, QueryTask.EXERCISE)]


async def test_a_query_past_the_hourly_budget_is_not_sent_and_reads_as_rate_limited() -> None:
    clock = FrozenClock()
    inner = _Embeddings()
    wrapped = BudgetedQueryEmbeddings(inner, _budget(clock, per_hour=1))

    first = await wrapped.embed_query("one", acting_user_id=_USER)
    refused = await wrapped.embed_query("two", acting_user_id=_USER)
    clock.advance(timedelta(hours=1))
    later = await wrapped.embed_query("three", acting_user_id=_USER)

    assert isinstance(first, Embedded)
    assert refused == RateLimited()
    assert isinstance(later, Embedded)
    assert [text for text, _, _ in inner.sent] == ["one", "three"]


async def test_concurrent_queries_past_the_in_flight_limit_are_refused() -> None:
    inner = _Embeddings(delay=0.05)
    wrapped = BudgetedQueryEmbeddings(inner, _budget(FrozenClock(), per_hour=10, in_flight=1))

    outcomes = await asyncio.gather(
        wrapped.embed_query("one", acting_user_id=_USER),
        wrapped.embed_query("two", acting_user_id=_USER),
    )

    assert [isinstance(outcome, Embedded) for outcome in outcomes] == [True, False]
    assert len(inner.sent) == 1


async def test_another_athletes_budget_is_separate() -> None:
    inner = _Embeddings()
    wrapped = BudgetedQueryEmbeddings(inner, _budget(FrozenClock(), per_hour=1))

    await wrapped.embed_query("one", acting_user_id=_USER)
    other = await wrapped.embed_query("one", acting_user_id=uuid4())

    assert isinstance(other, Embedded)
