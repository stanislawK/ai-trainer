"""The exercise card's image call is charged to the athlete's model-call budget (ADR-0029,
ADR-0031 *Picture on the card*, #643). A refused call is not sent, so it writes no `llm_calls`
row, and reads as `rate_limited`, which the card turns into its text-only fallback."""

from uuid import UUID, uuid4

from ai_trainer.application.budgeted_gateways import BudgetedImageGeneration
from ai_trainer.application.model_budget import ModelCallBudget
from ai_trainer.domain.media_generation import (
    GeneratedImage,
    ImageGenerationFailed,
)
from tests.ingestion_support import FrozenClock

_USER = uuid4()
_IMAGE = GeneratedImage(data=b"png", media_type="image/png")


class _Images:
    def __init__(self) -> None:
        self.sent: list[tuple[UUID, str]] = []

    async def generate(
        self, *, user_id: UUID, prompt: str
    ) -> GeneratedImage | ImageGenerationFailed:
        self.sent.append((user_id, prompt))
        return _IMAGE


async def test_an_allowed_image_call_reaches_the_generator_as_the_athlete() -> None:
    inner = _Images()
    wrapped = BudgetedImageGeneration(
        inner, ModelCallBudget(FrozenClock(), per_hour=1, in_flight=1)
    )

    assert await wrapped.generate(user_id=_USER, prompt="draw") == _IMAGE
    assert inner.sent == [(_USER, "draw")]


async def test_budgeted_image_generation_refused_is_not_sent() -> None:
    inner = _Images()
    wrapped = BudgetedImageGeneration(
        inner, ModelCallBudget(FrozenClock(), per_hour=1, in_flight=1)
    )

    await wrapped.generate(user_id=_USER, prompt="one")
    refused = await wrapped.generate(user_id=_USER, prompt="two")

    assert refused == ImageGenerationFailed(reason="rate_limited")
    assert inner.sent == [(_USER, "one")]
