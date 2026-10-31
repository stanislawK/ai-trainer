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
from ai_trainer.domain.sports.base import ExtractionQuestion, SessionExtraction


@dataclass(frozen=True, slots=True)
class ReplyContext:
    """What the router and every specialist see (ADR-0008): the message, the recent turns
    before it, the athlete's sports and goals, and the time context (ADR-0014). `now` is a UTC
    moment from the `Clock` port; `timezone` is the athlete's. `conversation_id` names the
    thread the message is in, so its calls can share one upstream (ADR-0026 step 4)."""

    user_id: UUID
    conversation_id: UUID
    message: str
    history: Sequence[ChatMessage]
    athlete_sports: Sequence[str]
    goals: Sequence[Goal]
    now: datetime
    timezone: str


@dataclass(frozen=True, slots=True)
class Routing:
    """The router's intents, in message order; empty when the call did not succeed.
    `plausible_sports` are the athlete's sports still in play for the message, most likely
    first (B23): Jev's reading when it gave one, else the sports of the router's own
    `log_session` intents. `sport_choice` is set when the message is one log whose sport stays
    split between those loggable sports: the athlete is asked which, and the intent carries the
    likeliest one for a reply that cannot wait (B17, ADR-0015)."""

    intents: Sequence[Intent]
    outcome: LlmCallOutcome
    plausible_sports: tuple[str, ...] = ()
    sport_choice: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateRef:
    id: str
    version: int


@dataclass(frozen=True, slots=True)
class FixedReply:
    """A reply sent as written, with no model call, and the file it came from."""

    text: str
    template: TemplateRef


@dataclass(frozen=True, slots=True)
class Extraction:
    """A sport extraction's draft, or the question it asks instead (ticket #164), `None` when
    the call did not succeed, and the template that made it (ADR-0008: the `log_session`
    specialist is the plugin's extraction template)."""

    output: SessionExtraction | ExtractionQuestion | None
    outcome: LlmCallOutcome
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

    async def extract(
        self,
        context: ReplyContext,
        sport_id: str,
        *,
        recent: Sequence[str] = (),
        answers: Sequence[str] = (),
        may_ask: bool = True,
    ) -> Extraction:
        """The sport's extraction template run on the message; the sport must name one.
        `recent` is the sport's context from the athlete's recent sessions. The template may
        return its question instead of a draft, unless the run `may_ask` not or carries the
        option IDs the athlete picked on it (`answers`): then it can only draft (ticket
        #164)."""
        ...

    def draft_ready(self) -> FixedReply:
        """The line that introduces a draft card, sent as written with no model call."""
        ...

    def draft_failed(self) -> FixedReply:
        """The line in place of a sport's draft whose extraction failed (ticket #161)."""
        ...

    def not_yet(self) -> FixedReply:
        """The skeleton's reply for an intent whose specialist hasn't shipped (ADR-0008)."""
        ...

    def sport_question(self) -> FixedReply:
        """The question above the choice card that asks which sport a log was (B23)."""
        ...

    def reading_question(self) -> FixedReply:
        """The question above the choice card that asks how to read a log its extraction
        couldn't read safely (B17, ticket #164)."""
        ...

    def same_session_question(self) -> FixedReply:
        """The question on a draft's card that asks whether it is the same session as an
        earlier one that day (B16, ticket #166)."""
        ...

    def exercise_question(self) -> FixedReply:
        """The question on a draft's card that asks which exercise a typed name is (B37, ticket
        #397)."""
        ...
