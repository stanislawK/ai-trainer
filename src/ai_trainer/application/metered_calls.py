"""Each `llm_calls` row also becomes a model-call metric, from the same values (ADR-0030)."""

from uuid import UUID

from ai_trainer.application.ports.llm_calls import LlmCallsRepositoryPort
from ai_trainer.application.ports.metrics import MetricsPort
from ai_trainer.domain.llm_calls import LlmCall, NewLlmCall


class MeteredLlmCallsRepository:
    """Implements `LlmCallsRepositoryPort`. The metric is recorded before the row is written,
    so a call is counted even when the write fails, and recording never raises, so the row is
    written even when the metric fails."""

    def __init__(self, inner: LlmCallsRepositoryPort, metrics: MetricsPort) -> None:
        self._inner = inner
        self._metrics = metrics

    async def record(self, call: NewLlmCall) -> LlmCall:
        self._metrics.model_call(call)
        return await self._inner.record(call)

    async def list_for_user(self, user_id: UUID) -> list[LlmCall]:
        return await self._inner.list_for_user(user_id)
