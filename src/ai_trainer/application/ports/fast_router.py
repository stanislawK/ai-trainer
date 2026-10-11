from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ai_trainer.application.ports.conversation import ReplyContext
from ai_trainer.domain.conversation import Intent


@dataclass(frozen=True, slots=True)
class FastRoute:
    """What the fast path made of a message. `intents` is the one confident intent, or `None`
    for the LLM router to decide: several requests, low confidence, `unclear`, a split sport,
    a timeout or any failure. `plausible_sports` is the athlete's sports that stay in play,
    most likely first (B23), or `None` when the fast path could not tell."""

    intents: Sequence[Intent] | None
    plausible_sports: tuple[str, ...] | None


class FastRouterPort(Protocol):
    """A fast path before the LLM router (ADR-0008 amendment, ADR-0020): a message with one
    confident intent is routed without the router's model call."""

    async def route(self, context: ReplyContext) -> FastRoute:
        """Never raises: any failure is `FastRoute(None, None)`."""
        ...
