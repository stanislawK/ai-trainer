import asyncio
import time
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel
from pydantic_ai import Agent, ModelMessage, ModelResponse
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.openrouter import OpenRouterProvider

from ai_trainer.application.ports.llm_calls import LlmCallsRepositoryPort
from ai_trainer.application.ports.llm_gateway import LlmGatewayResult
from ai_trainer.domain.llm_calls import LlmCallOutcome, NewLlmCall
from ai_trainer.settings import Settings

TIMEOUT_MESSAGE = "The AI assistant is taking too long to respond. Please try again in a moment."
ERROR_MESSAGE = "Something went wrong talking to the AI assistant. Please try again."


class OpenRouterGateway:
    """Implements `LlmGatewayPort` with Pydantic AI (ADR-0007).

    Cost comes from OpenRouter's own `usage.cost`, surfaced on
    `ModelResponse.provider_details['cost']` — not `result.usage.cost`, which for OpenRouter is a
    `genai-prices` static-table estimate that never reflects OpenRouter's real accounted figure
    (ADR-0018 invariant 6; see the ADR for how this was verified).
    """

    def __init__(self, settings: Settings, calls_repository: LlmCallsRepositoryPort) -> None:
        self._provider = OpenRouterProvider(api_key=settings.openrouter_api_key.get_secret_value())
        self._timeout_seconds = settings.llm_call_timeout_seconds
        self._calls_repository = calls_repository
        # A placeholder model: every real call overrides it via `model=` below. Tests substitute
        # TestModel/FunctionModel through `agent.override()` (ADR-0007) on this same instance.
        self.agent: Agent[None, str] = Agent(TestModel())

    async def run[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        template_id: str,
        template_version: int,
        model_id: str,
        output_type: type[OutputT],
        instructions: str,
        prompt: str,
    ) -> LlmGatewayResult[OutputT]:
        model = OpenRouterModel(model_id, provider=self._provider)
        model_settings = OpenRouterModelSettings(openrouter_cache_instructions=True)
        started = time.monotonic()

        async def _fail(outcome: LlmCallOutcome, message: str) -> LlmGatewayResult[OutputT]:
            await self._record(
                user_id,
                template_id,
                template_version,
                model_id,
                input_tokens=0,
                output_tokens=0,
                cost=None,
                latency_ms=_elapsed_ms(started),
                outcome=outcome,
            )
            return LlmGatewayResult(output=None, friendly_error=message, outcome=outcome)

        try:
            result = await asyncio.wait_for(
                self.agent.run(
                    prompt,
                    model=model,
                    output_type=output_type,
                    instructions=instructions,
                    model_settings=model_settings,
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return await _fail(LlmCallOutcome.TIMEOUT, TIMEOUT_MESSAGE)
        except Exception:
            # Every model/provider failure becomes a friendly message, never a stack trace
            # (ADR-0007).
            return await _fail(LlmCallOutcome.ERROR, ERROR_MESSAGE)

        cost = _sum_cost(result.all_messages())
        await self._record(
            user_id,
            template_id,
            template_version,
            model_id,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cost=cost,
            latency_ms=_elapsed_ms(started),
            outcome=LlmCallOutcome.SUCCESS,
        )
        return LlmGatewayResult(
            output=result.output, friendly_error=None, outcome=LlmCallOutcome.SUCCESS
        )

    async def _record(
        self,
        user_id: UUID,
        template_id: str,
        template_version: int,
        model_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cost: Decimal | None,
        latency_ms: int,
        outcome: LlmCallOutcome,
    ) -> None:
        await self._calls_repository.record(
            NewLlmCall(
                user_id=user_id,
                template_id=template_id,
                template_version=template_version,
                model=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=cost,
                latency_ms=latency_ms,
                outcome=outcome,
            )
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _sum_cost(messages: list[ModelMessage]) -> Decimal | None:
    """Sums `provider_details['cost']` across every model response in the run.

    A single `agent.run()` call can still make more than one model request — Pydantic AI retries
    output validation once by default — so `result.response` (the last response only) would
    silently undercount cost on a retried call. `None` when no response carried a cost at all.

    Each cost is converted to `Decimal` before summing, not after: summing the raw floats first
    (e.g. `0.1 + 0.2 == 0.30000000000000004`) would bake that binary rounding error into the
    stored figure, where converting each addend through its clean decimal `repr` first and only
    then summing avoids it.
    """
    costs: list[Decimal] = []
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        raw_cost = (message.provider_details or {}).get("cost")
        if raw_cost is not None:
            costs.append(Decimal(str(raw_cost)))
    return sum(costs, Decimal(0)) if costs else None
