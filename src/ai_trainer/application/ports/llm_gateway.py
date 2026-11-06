from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from ai_trainer.domain.knowledge_search import CitedSource
from ai_trainer.domain.llm_calls import LlmCallOutcome, ProviderPreferences, ReasoningEffort


@dataclass(frozen=True, slots=True)
class LlmGatewayResult[OutputT]:
    """Never raises for a model/provider failure: `output` or `friendly_error` is set (ADR-0007)."""

    output: OutputT | None
    friendly_error: str | None
    outcome: LlmCallOutcome


@dataclass(frozen=True, slots=True)
class TextChunk:
    text: str


@dataclass(frozen=True, slots=True)
class StreamEnd:
    """Always the last event of a stream that runs to its end; `output` is the full reply text.
    `sources` are the knowledge passages a tool loop's reply cited, only on success (PRD B8,
    ADR-0036 #428 amendment)."""

    result: LlmGatewayResult[str]
    sources: tuple[CitedSource, ...] = ()


type LlmStreamEvent = TextChunk | StreamEnd


@dataclass(frozen=True, slots=True)
class ToolLoop:
    """A streamed reply that may call the gateway's tools before it answers (ADR-0010):
    `max_requests` caps its model requests. Past the cap the call fails like any other."""

    max_requests: int


class LlmGatewayPort(Protocol):
    """The only way the application layer reaches a model; Pydantic AI stays in `llm/` (ADR-0007)"""

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
        """`session_id` is an opaque per-conversation key, never user identity (ADR-0026
        invariant 4); `timeout_seconds` overrides the gateway's default for this call. Several
        `output_type`s leave the model to pick which one it returns (ticket #164)."""
        ...

    def stream(
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
        """Yields `TextChunk`s, then one `StreamEnd`; never raises for a model/provider failure.

        A consumer that stops early should close the stream (`contextlib.aclosing`) so the call's
        row is written promptly, as `cancelled`; it then gets no `StreamEnd`. After a timeout or
        error `StreamEnd.result.output` is `None`, even if chunks were already yielded.

        With `tool_loop`, the one call and its one row cover every model request of the loop,
        and `user_id` is the user its tools act for. `timeout_seconds` bounds each wait on the
        model, as the gateway's default does.
        """
        ...
