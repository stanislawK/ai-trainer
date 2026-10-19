import asyncio
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import cast
from uuid import UUID

import httpx2
from pydantic import BaseModel
from pydantic_ai import Agent, AgentRetries
from pydantic_ai.models.openrouter import (
    OpenRouterModel,
    OpenRouterModelSettings,
    OpenRouterProviderConfig,
    OpenRouterReasoning,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.result import StreamedRunResult

from ai_trainer.application.ports.llm_calls import LlmCallsRepositoryPort
from ai_trainer.application.ports.llm_gateway import (
    LlmGatewayResult,
    LlmStreamEvent,
    StreamEnd,
    TextChunk,
)
from ai_trainer.domain.llm_calls import LlmCallOutcome, ReasoningEffort
from ai_trainer.llm.call_accounting import CallAccountant, CallKey, CallUsage, sum_cost
from ai_trainer.llm.debug_log import CallRecorder, LlmDebugLog
from ai_trainer.settings import Settings

TIMEOUT_MESSAGE = "The AI assistant is taking too long to respond. Please try again in a moment."
ERROR_MESSAGE = "Something went wrong talking to the AI assistant. Please try again."
_FRIENDLY_ERRORS = {
    LlmCallOutcome.TIMEOUT: TIMEOUT_MESSAGE,
    LlmCallOutcome.ERROR: ERROR_MESSAGE,
    LlmCallOutcome.CANCELLED: ERROR_MESSAGE,
}


class _Done:
    """Marks a stream that ended normally."""


_DONE = _Done()


@dataclass
class _StreamState:
    """The producer's run, shared so usage can be read however the stream ends."""

    streamed: StreamedRunResult[None, str] | None = None


class OpenRouterGateway:
    """Implements `LlmGatewayPort` with Pydantic AI (ADR-0007).

    Cost comes from OpenRouter's own `usage.cost`, surfaced on
    `ModelResponse.provider_details['cost']` — not `result.usage.cost`, which for OpenRouter is a
    `genai-prices` static-table estimate that never reflects OpenRouter's real accounted figure
    (ADR-0018 invariant 6; see the ADR for how this was verified).
    """

    def __init__(
        self,
        settings: Settings,
        calls_repository: LlmCallsRepositoryPort,
        *,
        http_client: httpx2.AsyncClient | None = None,
        debug_log: LlmDebugLog | None = None,
    ) -> None:
        self._provider = OpenRouterProvider(
            api_key=settings.openrouter_api_key.get_secret_value(), http_client=http_client
        )
        self._accountant = CallAccountant(
            calls_repository, timeout_seconds=settings.llm_call_timeout_seconds
        )
        self._debug_log = debug_log
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
        temperature: float | None = None,
        output_retries: int | None = None,
        upstream_provider: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> LlmGatewayResult[OutputT]:
        model = OpenRouterModel(model_id, provider=self._provider)
        model_settings = OpenRouterModelSettings(openrouter_cache_instructions=True)
        if temperature is not None:
            model_settings["temperature"] = temperature
        if upstream_provider is not None:
            model_settings["openrouter_provider"] = pinned_upstream(upstream_provider)
        if reasoning_effort is not None:
            model_settings["openrouter_reasoning"] = reasoning_at(reasoning_effort)
        recorder = self._recorder(template_id, template_version, model_id)
        attempt = await self._accountant.attempt(
            CallKey(user_id, template_id, template_version, model_id),
            self.agent.run(
                prompt,
                model=model,
                output_type=output_type,
                instructions=instructions,
                model_settings=model_settings,
                retries=AgentRetries(output=output_retries) if output_retries is not None else None,
                capabilities=[recorder] if recorder is not None else None,
            ),
            usage=lambda result: CallUsage(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cost=sum_cost(result.all_messages()),
            ),
        )
        if attempt.value is None:
            friendly_error = _FRIENDLY_ERRORS[attempt.outcome]
            return LlmGatewayResult(
                output=None, friendly_error=friendly_error, outcome=attempt.outcome
            )
        return LlmGatewayResult(
            output=attempt.value.output, friendly_error=None, outcome=LlmCallOutcome.SUCCESS
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
    ) -> AsyncGenerator[LlmStreamEvent]:
        """Streams a text reply; writes exactly one `llm_calls` row however the stream ends.

        A producer task owns the model stream and feeds a queue; this generator only reads the
        queue. Pydantic AI's stream holds an anyio cancel scope that must be entered and left in
        one task, so keeping it out of the generator makes an abandoned or cancelled consumer
        safe to finalise from any task.

        The timeout applies to each wait on the model (the first response, then each chunk), not
        to the whole reply, so a long answer that keeps flowing is never cut off and time the
        consumer spends between chunks never counts against it.
        """
        started = time.monotonic()
        queue: asyncio.Queue[str | Exception | _Done] = asyncio.Queue()
        state = _StreamState()
        recorder = self._recorder(template_id, template_version, model_id)
        producer = asyncio.create_task(
            self._pump(queue, state, model_id, instructions, prompt, recorder)
        )
        # Stays `CANCELLED` only if the consumer closes the generator at a `yield`.
        outcome = LlmCallOutcome.CANCELLED
        parts: list[str] = []
        friendly_error: str | None = None

        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=self._accountant.timeout_seconds
                    )
                except TimeoutError:
                    outcome, friendly_error = LlmCallOutcome.TIMEOUT, TIMEOUT_MESSAGE
                    break
                if isinstance(item, Exception):
                    # Every model/provider failure becomes a friendly message, never a stack
                    # trace (ADR-0007).
                    outcome, friendly_error = LlmCallOutcome.ERROR, ERROR_MESSAGE
                    break
                if isinstance(item, _Done):
                    outcome = LlmCallOutcome.SUCCESS
                    break
                parts.append(item)
                yield TextChunk(item)
        finally:
            await self._accountant.shielded(
                self._finish_stream(
                    producer,
                    state,
                    user_id,
                    template_id,
                    template_version,
                    model_id,
                    started=started,
                    outcome=outcome,
                )
            )

        output = "".join(parts) if outcome is LlmCallOutcome.SUCCESS else None
        yield StreamEnd(
            LlmGatewayResult(output=output, friendly_error=friendly_error, outcome=outcome)
        )

    async def _pump(
        self,
        queue: asyncio.Queue[str | Exception | _Done],
        state: _StreamState,
        model_id: str,
        instructions: str,
        prompt: str,
        recorder: CallRecorder | None,
    ) -> None:
        model = OpenRouterModel(model_id, provider=self._provider)
        model_settings = OpenRouterModelSettings(openrouter_cache_instructions=True)
        try:
            async with self.agent.run_stream(
                prompt,
                model=model,
                output_type=str,
                instructions=instructions,
                model_settings=model_settings,
                capabilities=[recorder] if recorder is not None else None,
            ) as streamed:
                state.streamed = streamed
                async for text in streamed.stream_text(delta=True, debounce_by=None):
                    if recorder is not None:
                        recorder.streamed(text)
                    await queue.put(text)
        except Exception as exc:
            await queue.put(exc)
            return
        await queue.put(_DONE)

    def _recorder(
        self, template_id: str, template_version: int, model_id: str
    ) -> CallRecorder | None:
        if self._debug_log is None:
            return None
        return self._debug_log.recorder(
            template_id=template_id, template_version=template_version, model=model_id
        )

    async def _finish_stream(
        self,
        producer: asyncio.Task[None],
        state: _StreamState,
        user_id: UUID,
        template_id: str,
        template_version: int,
        model_id: str,
        *,
        started: float,
        outcome: LlmCallOutcome,
    ) -> None:
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)
        usage = CallUsage()
        if state.streamed is not None:
            usage = CallUsage(
                input_tokens=state.streamed.usage.input_tokens,
                output_tokens=state.streamed.usage.output_tokens,
                cost=sum_cost(state.streamed.all_messages()),
            )
        await self._accountant.write(
            CallKey(user_id, template_id, template_version, model_id), usage, started, outcome
        )


def reasoning_at(effort: ReasoningEffort) -> OpenRouterReasoning:
    """OpenRouter's `reasoning` block at `effort` (ADR-0026 step 3). Pydantic AI 2.55 types
    `effort` without `max`, which glm-5.3-flash takes, and passes the value through as given."""
    return cast(OpenRouterReasoning, {"effort": effort})


def pinned_upstream(slug: str) -> OpenRouterProviderConfig:
    """OpenRouter tries `slug` first and falls back to its other endpoints only when that one
    fails, so one template sees one serving stack instead of a random quantization per call."""
    return OpenRouterProviderConfig(order=[slug], allow_fallbacks=True)
