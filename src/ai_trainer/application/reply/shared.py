"""What every part of the reply service shares: the events a reply streams, the services it
runs on, the athlete's own message it answers and the context its model calls read."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import UUID

from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.application.ports.chat import ChatRepositoryPort, ConversationRepositoryPort
from ai_trainer.application.ports.clarifications import ClarificationRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.conversation import (
    ConversationModelPort,
    ReplyContext,
    TemplateRef,
)
from ai_trainer.application.ports.drafts import SessionDraftRepositoryPort
from ai_trainer.application.ports.fast_router import FastRouterPort
from ai_trainer.application.ports.metrics import NO_METRICS, MetricsPort
from ai_trainer.application.ports.onboarding import OnboardingRepositoryPort
from ai_trainer.application.ports.safety_screen import SafetyScreenPort
from ai_trainer.application.ports.training import TrainingSessionRepositoryPort
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.clarification import Clarification, StoredClarification
from ai_trainer.domain.conversation import IntentKind
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.registry import SportRegistry
from ai_trainer.domain.training import SessionDraft, StoredDraft

LOG_KIND: IntentKind = "log_session"
# Streamed between two parts of one reply, which are saved as two messages.
PART_BREAK = "\n\n"
# Each reply part is stamped this far after what it follows, so it sorts right under the
# message it answers even when the athlete sent another message before it came through.
TICK = timedelta(microseconds=1)


class MessageNotFoundError(Exception):
    """Raised for a message that doesn't exist, isn't the user's, or isn't theirs to answer."""


class FailedPartNotFoundError(Exception):
    """Raised for a retry of a reply part that isn't the user's failed draft, or was already
    retried (ticket #161)."""


@dataclass(frozen=True, slots=True)
class ReplyPhase:
    """The step the reply is on, so the chat can name it before the first token (F14)."""

    phase: Literal["routing", "replying"]


@dataclass(frozen=True, slots=True)
class ReplyChunk:
    text: str


@dataclass(frozen=True, slots=True)
class ReplyDone:
    """The whole reply, as saved: one message per part, in the order they were streamed, the
    pending drafts made from the message, and the questions it asks (ADR-0015)."""

    messages: Sequence[ChatMessage]
    drafts: Sequence[StoredDraft] = ()
    clarifications: Sequence[StoredClarification] = ()


@dataclass(frozen=True, slots=True)
class ReplyFailed:
    """The router or a specialist timed out or failed; nothing was saved."""

    outcome: LlmCallOutcome


type ReplyEvent = ReplyPhase | ReplyChunk | ReplyDone | ReplyFailed


@dataclass(frozen=True, slots=True)
class ReplyServices:
    chat: ChatRepositoryPort
    conversations: ConversationRepositoryPort
    drafts: SessionDraftRepositoryPort
    clarifications: ClarificationRepositoryPort
    sessions: TrainingSessionRepositoryPort
    sports: SportRegistry
    profile: OnboardingRepositoryPort
    conversation: ConversationModelPort
    fast_router: FastRouterPort
    safety_screen: SafetyScreenPort
    clock: ClockPort
    # The router and every specialist see this many turns before the message, from its own
    # conversation only (ADR-0008, ADR-0021).
    history_turns: int
    # A log this close after the latest log of a session in its sport is offered as an addition
    # to it (`session_merge_window_min`, B16).
    merge_window: timedelta
    # A question offers at most this many options (`max_clarification_options`, ADR-0015).
    max_clarification_options: int
    # A sport's extraction reads its context from this many of the athlete's latest sessions in
    # it (`extract_recent_sessions`, ticket #164).
    recent_sessions: int
    # Times each fresh log turn to its draft card (ADR-0030, G14).
    metrics: MetricsPort = NO_METRICS
    # Records the athlete's choice-card answers as feedback events (ADR-0034).
    feedback: FeedbackRecorder | None = None


@dataclass(frozen=True, slots=True)
class Answer:
    """One part of a reply as a specialist made it, before it is saved."""

    text: str
    template: TemplateRef
    sports: tuple[str, ...]
    # The draft an extraction part made, with its sport and payload version.
    draft: tuple[str, SessionDraft, int] | None = None
    # The question an extraction part asked instead of a draft (ticket #164).
    question: Clarification | None = None


def extractable(sports: SportRegistry) -> frozenset[str]:
    """The sports whose plugin names an extraction template (ADR-0006)."""
    return frozenset(plugin.id for plugin in sports.all() if plugin.extraction is not None)


async def find_message(
    user_id: UUID,
    message_id: UUID,
    *,
    chat: ChatRepositoryPort,
    conversations: ConversationRepositoryPort,
) -> ChatMessage:
    """The athlete's own message, the only kind that gets a reply. One in a conversation past
    its retention window is not found, though the purge hasn't removed it yet (ADR-0021)."""
    message = await chat.get(user_id, message_id)
    if (
        message is None
        or message.role is not ChatRole.USER
        or await conversations.get(user_id, message.conversation_id) is None
    ):
        raise MessageNotFoundError
    return message


async def reply_context(
    user_id: UUID, message: ChatMessage, timezone: str, services: ReplyServices
) -> ReplyContext:
    return ReplyContext(
        user_id=user_id,
        conversation_id=message.conversation_id,
        message=message.text,
        history=await services.chat.list_before(user_id, message, services.history_turns),
        athlete_sports=await services.profile.list_sports(user_id),
        goals=await services.profile.list_goals(user_id),
        now=services.clock.now(),
        timezone=timezone,
    )
