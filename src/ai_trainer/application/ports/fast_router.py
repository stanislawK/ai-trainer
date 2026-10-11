from collections.abc import Sequence
from typing import Protocol

from ai_trainer.application.ports.conversation import ReplyContext
from ai_trainer.domain.conversation import Intent


class FastRouterPort(Protocol):
    """A fast path before the LLM router (ADR-0008 amendment, ADR-0020): a message with one
    confident intent is routed without the router's model call."""

    async def route(self, context: ReplyContext) -> Sequence[Intent] | None:
        """The one intent, or `None` for the LLM router to decide: several requests, low
        confidence, `unclear`, a timeout or any failure. Never raises."""
        ...
