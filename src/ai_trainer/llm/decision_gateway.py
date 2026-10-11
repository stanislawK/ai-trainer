from typing import Any
from uuid import UUID

import httpx2
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.typesafe import TypeSafeModel
from pydantic_ai.providers.typesafe import TypeSafeProvider
from pydantic_ai.run import AgentRunResult

from ai_trainer.application.ports.decision_gateway import DecisionResult
from ai_trainer.application.ports.llm_calls import LlmCallsRepositoryPort
from ai_trainer.llm.call_accounting import CallAccountant, CallKey, CallUsage, sum_cost
from ai_trainer.settings import Settings

# The TypeSafe SDK appends `/v1/systemone`, which OpenRouter serves in TypeSafe's wire format.
OPENROUTER_SYSTEM_ONE_BASE_URL = "https://openrouter.ai/api"


class OpenRouterDecisionGateway:
    """Implements `DecisionGatewayPort` with Pydantic AI's `TypeSafeModel`, pointed at
    OpenRouter and paid with the OpenRouter key (ADR-0020).

    `cost` stays `None`: OpenRouter's response carries `usage.cost`, but the TypeSafe SDK drops
    it and the row never stores a figure priced in code (ADR-0018 invariant 6).
    """

    def __init__(
        self,
        settings: Settings,
        calls_repository: LlmCallsRepositoryPort,
        *,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        provider = TypeSafeProvider(
            base_url=OPENROUTER_SYSTEM_ONE_BASE_URL,
            api_key=settings.openrouter_api_key.get_secret_value(),
            http_client=http_client,
        )
        self._model_id = settings.decision_model
        self._model = TypeSafeModel(self._model_id, provider=provider)
        self._accountant = CallAccountant(
            calls_repository, timeout_seconds=settings.decision_call_timeout_seconds
        )
        # A placeholder model: every real call overrides it via `model=` below. Tests substitute
        # a fake `DecisionModel` through `agent.override()` on this same instance.
        self.agent: Agent[None, Any] = Agent(TestModel())

    async def decide[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        decision_id: str,
        decision_version: int,
        output_type: type[OutputT],
        state: str,
    ) -> DecisionResult[OutputT]:
        attempt = await self._accountant.attempt(
            CallKey(user_id, decision_id, decision_version, self._model_id),
            self.agent.run(state, model=self._model, output_type=output_type),
            usage=_usage,
        )
        if attempt.value is None:
            return DecisionResult(output=None, outcome=attempt.outcome)
        details = attempt.value.response.provider_details or {}
        return DecisionResult(
            output=attempt.value.output,
            outcome=attempt.outcome,
            probabilities=details.get("probabilities", {}),
        )


def _usage(result: AgentRunResult[Any]) -> CallUsage:
    return CallUsage(
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost=sum_cost(result.all_messages()),
        model=result.response.model_name,
    )
