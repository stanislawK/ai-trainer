"""The text of a run that may call tools before it answers (ADR-0010, ticket #421), streamed from
every model request in turn. `run_stream` would take the first text as the final output, so a tool
call after "Let me check." would never run (Pydantic AI, `docs/agent.md`); `iter` runs it all."""

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic_ai import Agent, AgentRun, PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta
from pydantic_ai.messages import AgentStreamEvent

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
    between requests and no leading blank text. Whether any text came."""
    said = False
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
    return said
