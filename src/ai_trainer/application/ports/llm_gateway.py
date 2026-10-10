from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from ai_trainer.domain.llm_calls import LlmCallOutcome


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
    """Always the last event of a stream that runs to its end; `output` is the full reply text."""

    result: LlmGatewayResult[str]


type LlmStreamEvent = TextChunk | StreamEnd


class LlmGatewayPort(Protocol):
    """The only way the application layer reaches a model; Pydantic AI stays in `llm/` (ADR-0007)"""

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
    ) -> LlmGatewayResult[OutputT]: ...

    def stream(
        self,
        *,
        user_id: UUID,
        template_id: str,
        template_version: int,
        model_id: str,
        instructions: str,
        prompt: str,
    ) -> AsyncGenerator[LlmStreamEvent]:
        """Yields `TextChunk`s, then one `StreamEnd`; never raises for a model/provider failure.

        A consumer that stops early should close the stream (`contextlib.aclosing`) so the call's
        row is written promptly, as `cancelled`; it then gets no `StreamEnd`. After a timeout or
        error `StreamEnd.result.output` is `None`, even if chunks were already yielded.
        """
        ...
