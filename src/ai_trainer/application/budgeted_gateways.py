"""Wraps the two model gateways with one `ModelCallBudget` (ADR-0029).

A refused call is not delegated, so nothing is sent and no `llm_calls` row is written.
The athlete-facing sentence lives in the reply template, not here.
"""

from collections.abc import AsyncGenerator
from uuid import UUID

from pydantic import BaseModel

from ai_trainer.application.model_budget import ModelCallBudget
from ai_trainer.application.ports.decision_gateway import DecisionGatewayPort, DecisionResult
from ai_trainer.application.ports.embeddings import QueryEmbeddingPort
from ai_trainer.application.ports.llm_gateway import (
    LlmGatewayPort,
    LlmGatewayResult,
    LlmStreamEvent,
    StreamEnd,
    ToolLoop,
)
from ai_trainer.domain.embeddings import EmbeddingOutcome, QueryTask, RateLimited
from ai_trainer.domain.llm_calls import LlmCallOutcome, ProviderPreferences, ReasoningEffort


def _refused_text() -> LlmGatewayResult[str]:
    return LlmGatewayResult(output=None, friendly_error=None, outcome=LlmCallOutcome.RATE_LIMITED)


class BudgetedLlmGateway:
    """Implements `LlmGatewayPort`. Constructed only in the composition root."""

    def __init__(self, inner: LlmGatewayPort, budget: ModelCallBudget) -> None:
        self._inner = inner
        self._budget = budget

    async def run[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        template_id: str,
        template_version: int,
        model_id: str,
        output_type: type[OutputT] | tuple[type[OutputT], ...],
        instructions: str,
        prompt: str,
        temperature: float | None = None,
        output_retries: int | None = None,
        provider: ProviderPreferences | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> LlmGatewayResult[OutputT]:
        async with self._budget.hold(user_id) as allowed:
            if not allowed:
                return LlmGatewayResult(
                    output=None, friendly_error=None, outcome=LlmCallOutcome.RATE_LIMITED
                )
            return await self._inner.run(
                user_id=user_id,
                template_id=template_id,
                template_version=template_version,
                model_id=model_id,
                output_type=output_type,
                instructions=instructions,
                prompt=prompt,
                temperature=temperature,
                output_retries=output_retries,
                provider=provider,
                reasoning_effort=reasoning_effort,
                session_id=session_id,
                timeout_seconds=timeout_seconds,
            )

    async def stream(
        self,
        *,
        user_id: UUID,
        template_id: str,
        template_version: int,
        model_id: str,
        instructions: str,
        prompt: str,
        tool_loop: ToolLoop | None = None,
        provider: ProviderPreferences | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> AsyncGenerator[LlmStreamEvent]:
        # A tool loop holds one slot and counts once; `ToolLoop.max_requests` bounds its requests.
        async with self._budget.hold(user_id) as allowed:
            if not allowed:
                yield StreamEnd(result=_refused_text())
                return
            async for event in self._inner.stream(
                user_id=user_id,
                template_id=template_id,
                template_version=template_version,
                model_id=model_id,
                instructions=instructions,
                prompt=prompt,
                tool_loop=tool_loop,
                provider=provider,
                reasoning_effort=reasoning_effort,
                session_id=session_id,
                timeout_seconds=timeout_seconds,
            ):
                yield event


class BudgetedQueryEmbeddings:
    """Implements `QueryEmbeddingPort`. Shares the text gateway's budget, for the embedding calls
    that run outside a tool loop's slot: the exercise search on the log path (ADR-0031, #429),
    where a log naming N exercises makes N calls at once. A refused call is not sent and reads as
    `RateLimited`, which the search turns into its fallback."""

    def __init__(self, inner: QueryEmbeddingPort, budget: ModelCallBudget) -> None:
        self._inner = inner
        self._budget = budget

    async def embed_query(
        self, text: str, *, acting_user_id: UUID, task: QueryTask = QueryTask.QUESTION
    ) -> EmbeddingOutcome:
        async with self._budget.hold(acting_user_id) as allowed:
            if not allowed:
                return RateLimited()
            return await self._inner.embed_query(text, acting_user_id=acting_user_id, task=task)


class BudgetedDecisionGateway:
    """Implements `DecisionGatewayPort`. Shares the text gateway's budget."""

    def __init__(self, inner: DecisionGatewayPort, budget: ModelCallBudget) -> None:
        self._inner = inner
        self._budget = budget

    async def decide[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        decision_id: str,
        decision_version: int,
        output_type: type[OutputT],
        state: str,
    ) -> DecisionResult[OutputT]:
        async with self._budget.hold(user_id) as allowed:
            if not allowed:
                return DecisionResult(output=None, outcome=LlmCallOutcome.RATE_LIMITED)
            return await self._inner.decide(
                user_id=user_id,
                decision_id=decision_id,
                decision_version=decision_version,
                output_type=output_type,
                state=state,
            )
