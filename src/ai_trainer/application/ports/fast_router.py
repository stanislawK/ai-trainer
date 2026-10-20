from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from ai_trainer.application.ports.conversation import ReplyContext
from ai_trainer.domain.conversation import Intent, IntentKind


@dataclass(frozen=True, slots=True)
class FastRoute:
    """What the fast path made of a message. `intents` is the one confident intent, or `None`
    for the LLM router to decide: several requests, low confidence, `unclear`, a split sport,
    a timeout or any failure. `plausible_sports` is the athlete's sports that stay in play,
    most likely first (B23), or `None` when the fast path could not tell. `single_intent` is
    the one request Jev is sure the message holds, even when its sport is too split to route
    on, so a follow-up log can take its sport from the conversation (ADR-0026 step 5)."""

    intents: Sequence[Intent] | None
    plausible_sports: tuple[str, ...] | None
    single_intent: IntentKind | None = None


class FastRouterPort(Protocol):
    """A fast path before the LLM router (ADR-0008 amendment, ADR-0020): a message with one
    confident intent is routed without the router's model call."""

    async def route(self, context: ReplyContext) -> FastRoute:
        """Never raises: any failure is `FastRoute(None, None)`."""
        ...
