from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.application.ports.llm_gateway import LlmStreamEvent
from ai_trainer.domain.chat import ChatMessage
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.llm_calls import LlmCallOutcome


@dataclass(frozen=True, slots=True)
class ReplyContext:
    """What the router and every specialist see (ADR-0008): the message, the recent turns
    before it, the athlete's sports and goals, and the time context (ADR-0014). `now` is a UTC
    moment from the `Clock` port; `timezone` is the athlete's."""

    user_id: UUID
    message: str
    history: Sequence[ChatMessage]
    athlete_sports: Sequence[str]
    goals: Sequence[Goal]
    now: datetime
    timezone: str


@dataclass(frozen=True, slots=True)
class Routing:
    """The router's intents, in message order; empty when the call did not succeed."""

    intents: Sequence[Intent]
    outcome: LlmCallOutcome


@dataclass(frozen=True, slots=True)
class TemplateRef:
    id: str
    version: int


@dataclass(frozen=True, slots=True)
class FixedReply:
    """A reply sent as written, with no model call, and the file it came from."""

    text: str
    template: TemplateRef


class ConversationModelPort(Protocol):
    """The router and the specialists behind one port, so the dispatcher stays in the
    application layer while templates and models stay in `llm/` (ADR-0003, ADR-0008)."""

    @property
    def chitchat_template(self) -> TemplateRef: ...

    async def route(self, context: ReplyContext) -> Routing: ...

    def stream_chitchat(self, context: ReplyContext) -> AsyncGenerator[LlmStreamEvent]:
        """The `chitchat` specialist's reply, streamed as the gateway streams it."""
        ...

    def not_yet(self) -> FixedReply:
        """The skeleton's reply for an intent whose specialist hasn't shipped (ADR-0008)."""
        ...
