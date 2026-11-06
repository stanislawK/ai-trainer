"""The text of a run that may call tools before it answers (ADR-0010, ticket #421), streamed from
every model request in turn. `run_stream` would take the first text as the final output, so a tool
call after "Let me check." would never run (Pydantic AI, `docs/agent.md`); `iter` runs it all."""

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from pydantic_ai import Agent, AgentRun, PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta
from pydantic_ai.messages import AgentStreamEvent, ModelMessage

from ai_trainer.domain.knowledge_search import CitedSource

logger = logging.getLogger(__name__)

# Between the text of two model requests, so "Let me check." and the answer don't run together.
REQUEST_BREAK = "\n\n"


def _text_of(event: AgentStreamEvent) -> str | None:
    """The text one stream event adds to the reply; a tool call or thinking adds none."""
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    return None


async def stream_run_text(run: AgentRun[Any, str], put: Callable[[str], Awaitable[None]]) -> bool:
    """Hands `put` each piece of text the run's model requests make, in order, with a break
    between requests and no leading blank text. Whether the last request said anything: a
    "Let me check." before the tools is no answer on its own."""
    said = request_said = False
    async for node in run:
        if not Agent.is_model_request_node(node):
            continue
        request_said = False
        async with node.stream(run.ctx) as events:
            async for event in events:
                text = _text_of(event)
                if not text or (not text.strip() and not request_said):
                    continue
                if said and not request_said:
                    await put(REQUEST_BREAK)
                said = request_said = True
                await put(text)
    return request_said


# Reads which knowledge passages a finished tool loop's reply cited (`mcp/citations.py`, ADR-0036
# #428 amendment). `main.py` hands it over with the toolset, since `llm` doesn't import `mcp`.
type CitationReader = Callable[[Sequence[ModelMessage]], tuple[CitedSource, ...]]


def read_citations(
    reader: CitationReader, messages: Sequence[ModelMessage]
) -> tuple[CitedSource, ...]:
    """The sources sit beside an answer the athlete already read, so a reader that breaks costs
    the sources, never the answer (#428 review)."""
    try:
        return reader(messages)
    except Exception:
        # No exception text or trace: either could quote a passage or the reply (ADR-0030).
        logger.warning("citations could not be read; the reply is kept without sources")
        return ()
