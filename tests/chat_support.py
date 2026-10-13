"""Fakes for the chat (tickets #78 and #82): an in-memory chat, a frozen clock, an athlete's
profile and a scripted router and chitchat specialist. Tests that mount the chat router only
because `/` is the signed-in home page use them too."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import takewhile
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from starlette.templating import Jinja2Templates

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.conversation import (
    Extraction,
    FixedReply,
    ReplyContext,
    Routing,
    TemplateRef,
)
from ai_trainer.application.ports.fast_router import FastRoute
from ai_trainer.application.ports.llm_gateway import (
    LlmGatewayResult,
    LlmStreamEvent,
    StreamEnd,
    TextChunk,
)
from ai_trainer.application.reply import ReplyServices
from ai_trainer.domain.chat import ChatMessage, ChatRole, Conversation
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.base import SessionExtraction, SportPlugin
from ai_trainer.domain.sports.climbing.extraction import ClimbingExtraction, ExtractedClimb
from ai_trainer.domain.sports.registry import SportRegistry, default_sport_registry
from ai_trainer.domain.training import (
    DraftConfirmedError,
    DraftDiscardedError,
    DraftNotFoundError,
    DraftStatus,
    SessionDraft,
    SessionNotFoundError,
    StoredDraft,
)
from ai_trainer.domain.users import User, UserStatus
from ai_trainer.web.chat import build_chat_router

FROZEN_NOW = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)

# Every app sport names an extraction template since #159, so a log only gets the "not yet"
# reply for one that doesn't: this test-only sport, registered after the app's own.
UNLOGGABLE_SPORT = "rowing"


def sports_with_rowing() -> SportRegistry:
    """The app's sports plus `UNLOGGABLE_SPORT`, which names no extraction template."""
    return SportRegistry(
        [
            *default_sport_registry().all(),
            SportPlugin(id=UNLOGGABLE_SPORT, label_key="sport.rowing.label", icon="route"),
        ]
    )


class FixedClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class InMemoryConversationRepository:
    """Hides conversations older than the retention window on read, as the database adapter
    does (ADR-0021); `clock` is public so a test can move time."""

    def __init__(self, clock: ClockPort | None = None, retention_days: int = 14) -> None:
        self.rows: list[tuple[UUID, Conversation]] = []
        self.clock: ClockPort = clock or FixedClock()
        self.retention = timedelta(days=retention_days)

    def _live(self, conversation: Conversation) -> bool:
        return conversation.last_message_at >= self.clock.now() - self.retention

    async def create(self, user_id: UUID, title: str | None, now: datetime) -> Conversation:
        conversation = Conversation(id=uuid4(), title=title, created_at=now, last_message_at=now)
        self.rows.append((user_id, conversation))
        return conversation

    async def get(self, user_id: UUID, conversation_id: UUID) -> Conversation | None:
        return next(
            (
                conversation
                for owner, conversation in self.rows
                if owner == user_id
                and conversation.id == conversation_id
                and self._live(conversation)
            ),
            None,
        )

    async def list_recent(self, user_id: UUID, limit: int) -> Sequence[Conversation]:
        own = [
            conversation
            for owner, conversation in self.rows
            if owner == user_id and self._live(conversation)
        ]
        return sorted(own, key=lambda conversation: conversation.last_message_at, reverse=True)[
            :limit
        ]

    async def purge_before(self, cutoff: datetime) -> int:
        kept = [(owner, c) for owner, c in self.rows if c.last_message_at >= cutoff]
        deleted = len(self.rows) - len(kept)
        self.rows = kept
        return deleted

    async def touch(self, user_id: UUID, conversation_id: UUID, when: datetime) -> None:
        self.rows = [
            (
                owner,
                conversation.model_copy(
                    update={"last_message_at": max(conversation.last_message_at, when)}
                )
                if owner == user_id and conversation.id == conversation_id
                else conversation,
            )
            for owner, conversation in self.rows
        ]


class InMemoryChatRepository:
    """Owns an in-memory `conversations` store the way the database holds both tables. A
    message's conversation must be one of its user's, as the foreign key and the application
    layer's lookup guarantee in production."""

    def __init__(self) -> None:
        self.rows: list[tuple[UUID, ChatMessage]] = []
        self.conversations = InMemoryConversationRepository()

    async def add(
        self,
        user_id: UUID,
        conversation_id: UUID,
        role: ChatRole,
        text: str,
        created_at: datetime,
        *,
        sports: Sequence[str] = (),
        template_id: str | None = None,
        template_version: int | None = None,
    ) -> ChatMessage:
        assert await self.conversations.get(user_id, conversation_id) is not None
        message = ChatMessage(
            id=uuid4(),
            conversation_id=conversation_id,
            role=role,
            text=text,
            created_at=created_at,
            sports=tuple(sports),
            template_id=template_id,
            template_version=template_version,
        )
        self.rows.append((user_id, message))
        return message

    async def start(self, user_id: UUID, title: str | None = "A chat") -> UUID:
        """A test's shortcut: a new conversation for `user_id`, created at `FROZEN_NOW`."""
        return (await self.conversations.create(user_id, title, FROZEN_NOW)).id

    async def conversation_of(self, user_id: UUID) -> UUID:
        """A test's shortcut: the first conversation `user_id` started, started now if none."""
        own = [conversation for owner, conversation in self.conversations.rows if owner == user_id]
        return own[0].id if own else await self.start(user_id)

    def _own(self, user_id: UUID) -> list[ChatMessage]:
        """Ordered by `created_at` like the real repository; ties keep insertion order."""
        own = [message for owner, message in self.rows if owner == user_id]
        return sorted(own, key=lambda message: message.created_at)

    def _thread(self, user_id: UUID, conversation_id: UUID) -> list[ChatMessage]:
        return [
            message for message in self._own(user_id) if message.conversation_id == conversation_id
        ]

    async def get(self, user_id: UUID, message_id: UUID) -> ChatMessage | None:
        return next((message for message in self._own(user_id) if message.id == message_id), None)

    async def list_recent(
        self, user_id: UUID, conversation_id: UUID, limit: int
    ) -> Sequence[ChatMessage]:
        return self._thread(user_id, conversation_id)[-limit:]

    async def list_before(
        self, user_id: UUID, before: ChatMessage, limit: int
    ) -> Sequence[ChatMessage]:
        thread = self._thread(user_id, before.conversation_id)
        return thread[: thread.index(before)][-limit:]

    async def list_replies(self, user_id: UUID, to: ChatMessage) -> Sequence[ChatMessage]:
        thread = self._thread(user_id, to.conversation_id)
        later = thread[thread.index(to) + 1 :]
        return list(takewhile(lambda message: message.role is ChatRole.ASSISTANT, later))

    def all_of(self, user_id: UUID) -> list[ChatMessage]:
        """Every message of `user_id`, in every conversation, oldest first."""
        return self._own(user_id)


class InMemoryProfile:
    """The athlete's sports and goals, the only parts of onboarding a reply reads."""

    def __init__(
        self,
        sports: Mapping[UUID, Sequence[str]] | None = None,
        goals: Mapping[UUID, Sequence[Goal]] | None = None,
    ) -> None:
        self.sports = dict(sports or {})
        self.goals = dict(goals or {})

    async def list_sports(self, user_id: UUID) -> Sequence[str]:
        return self.sports.get(user_id, [])

    async def list_goals(self, user_id: UUID) -> Sequence[Goal]:
        return self.goals.get(user_id, [])

    async def replace_sports(
        self,
        user_id: UUID,
        sport_ids: Sequence[str],
        *,
        general_goals_of: Sequence[str] = (),
        delete_goals_of: Sequence[str] = (),
    ) -> None:
        raise NotImplementedError

    async def replace_availability(
        self, user_id: UUID, minutes_by_weekday: Mapping[int, int]
    ) -> None:
        raise NotImplementedError

    async def list_availability(self, user_id: UUID) -> Mapping[int, int]:
        raise NotImplementedError

    async def replace_goals(self, user_id: UUID, goals: Sequence[Goal]) -> None:
        raise NotImplementedError

    async def set_onboarded_at(self, user_id: UUID, when: datetime | None) -> None:
        raise NotImplementedError

    async def finish_onboarding(self, user_id: UUID, timezone: str, when: datetime) -> None:
        raise NotImplementedError


class InMemoryDraftRepository:
    """`session_drafts` in memory. A draft's message must be its user's, as the foreign key and
    the reply's lookup guarantee in production."""

    def __init__(self) -> None:
        self.rows: list[tuple[UUID, StoredDraft]] = []
        self.confirmed_at: dict[UUID, datetime] = {}
        self.discarded_at: dict[UUID, datetime] = {}
        self.removed_sessions: list[UUID] = []

    async def add(
        self,
        user_id: UUID,
        conversation_id: UUID,
        message_id: UUID,
        sport_id: str,
        draft: SessionDraft,
        *,
        payload_version: int,
        created_at: datetime,
    ) -> StoredDraft:
        stored = StoredDraft(
            id=uuid4(),
            conversation_id=conversation_id,
            message_id=message_id,
            sport_id=sport_id,
            draft=draft,
            payload_version=payload_version,
            status="pending",
            created_at=created_at,
        )
        self.rows.append((user_id, stored))
        return stored

    async def list_shown(self, user_id: UUID, conversation_id: UUID) -> Sequence[StoredDraft]:
        return [
            draft
            for owner, draft in self.rows
            if owner == user_id and draft.conversation_id == conversation_id
        ]

    async def get(self, user_id: UUID, draft_id: UUID) -> StoredDraft:
        for owner, draft in self.rows:
            if owner == user_id and draft.id == draft_id:
                return draft
        raise DraftNotFoundError(draft_id)

    async def update(
        self, user_id: UUID, draft_id: UUID, draft: SessionDraft, *, payload_version: int
    ) -> StoredDraft:
        for index, (owner, stored) in enumerate(self.rows):
            if owner != user_id or stored.id != draft_id:
                continue
            if stored.status == "confirmed":
                raise DraftConfirmedError(draft_id)
            if stored.status == "discarded":
                raise DraftDiscardedError(draft_id)
            stored = stored.model_copy(update={"draft": draft, "payload_version": payload_version})
            self.rows[index] = (owner, stored)
            return stored
        raise DraftNotFoundError(draft_id)

    async def confirm(
        self, user_id: UUID, draft_id: UUID, *, confirmed_at: datetime
    ) -> StoredDraft:
        for index, (owner, draft) in enumerate(self.rows):
            if owner != user_id or draft.id != draft_id:
                continue
            if draft.status == "discarded":
                raise DraftDiscardedError(draft_id)
            if draft.status == "pending":
                draft = draft.model_copy(update={"status": "confirmed", "session_id": uuid4()})
                self.rows[index] = (owner, draft)
                self.confirmed_at[draft.id] = confirmed_at
            return draft
        raise DraftNotFoundError(draft_id)

    async def discard(
        self, user_id: UUID, draft_id: UUID, *, discarded_at: datetime
    ) -> StoredDraft:
        for index, (owner, draft) in enumerate(self.rows):
            if owner != user_id or draft.id != draft_id:
                continue
            if draft.status == "confirmed":
                raise DraftConfirmedError(draft_id)
            if draft.status == "pending":
                draft = draft.model_copy(update={"status": "discarded"})
                self.rows[index] = (owner, draft)
                self.discarded_at[draft.id] = discarded_at
            return draft
        raise DraftNotFoundError(draft_id)

    async def undo(self, user_id: UUID, session_id: UUID) -> StoredDraft:
        for index, (owner, draft) in enumerate(self.rows):
            if owner != user_id or draft.session_id != session_id:
                continue
            draft = draft.model_copy(update={"session_id": None})
            self.rows[index] = (owner, draft)
            self.removed_sessions.append(session_id)
            return draft
        raise SessionNotFoundError(session_id)

    def set_status(self, draft_id: UUID, status: DraftStatus) -> None:
        """A confirmed draft has its session, as a confirm gives it one."""
        update: dict[str, object] = {"status": status}
        if status == "confirmed":
            update["session_id"] = uuid4()
        self.rows = [
            (owner, draft.model_copy(update=update) if draft.id == draft_id else draft)
            for owner, draft in self.rows
        ]

    def all_of(self, user_id: UUID) -> list[StoredDraft]:
        return [draft for owner, draft in self.rows if owner == user_id]


NOT_YET_TEXT = "I can't do that one yet."
DRAFT_READY_TEXT = "Here's the session."
CHITCHAT_TEMPLATE = TemplateRef(id="chitchat", version=1)
NOT_YET_TEMPLATE = TemplateRef(id="not_yet", version=1)
DRAFT_READY_TEMPLATE = TemplateRef(id="draft_ready", version=1)
CLIMBING_EXTRACT_TEMPLATE = TemplateRef(id="climbing.extract", version=1)
# "I climbed 3 6A and 2 6B boulders", sent at `FROZEN_NOW` with no date (Europe/Warsaw).
THREE_6A_TWO_6B = ClimbingExtraction(
    occurred_on=date(2026, 10, 2),
    date_source="assumed_today",
    activities=[
        ExtractedClimb(kind="boulder", count=3, grade="6A"),
        ExtractedClimb(kind="boulder", count=2, grade="6B"),
    ],
)


@dataclass
class ScriptedConversation:
    """Routes every message to `intents` (or fails with `route_outcome`) and streams
    `chunks` from chitchat (or fails with `chitchat_outcome` after them). Each list entry
    scripts one call; the last one repeats. Records every context it was given."""

    intents: list[list[Intent]] = field(default_factory=lambda: [[Intent(kind="chitchat")]])
    route_outcomes: list[LlmCallOutcome] = field(default_factory=lambda: [LlmCallOutcome.SUCCESS])
    chunks: list[str] = field(default_factory=lambda: ["Hey! ", "Good to hear from you."])
    chitchat_outcomes: list[LlmCallOutcome] = field(
        default_factory=lambda: [LlmCallOutcome.SUCCESS]
    )
    # The router answers `route_delay` seconds after `route_after` is set (at once when unset).
    route_delay: float = 0.0
    route_after: asyncio.Event | None = None
    # What each extraction call returns: a draft, or `None` with `extract_outcomes`' outcome.
    extractions: list[SessionExtraction | None] = field(default_factory=lambda: [THREE_6A_TWO_6B])
    extract_outcomes: list[LlmCallOutcome] = field(default_factory=lambda: [LlmCallOutcome.SUCCESS])
    routed: list[ReplyContext] = field(default_factory=list)
    chitchatted: list[ReplyContext] = field(default_factory=list)
    extracted: list[tuple[ReplyContext, str]] = field(default_factory=list)
    closed_streams: int = 0

    @property
    def chitchat_template(self) -> TemplateRef:
        return CHITCHAT_TEMPLATE

    async def route(self, context: ReplyContext) -> Routing:
        call = len(self.routed)
        self.routed.append(context)
        if self.route_after is not None:
            await self.route_after.wait()
        await asyncio.sleep(self.route_delay)
        outcome = self.route_outcomes[min(call, len(self.route_outcomes) - 1)]
        if outcome is not LlmCallOutcome.SUCCESS:
            return Routing(intents=(), outcome=outcome)
        return Routing(intents=self.intents[min(call, len(self.intents) - 1)], outcome=outcome)

    async def stream_chitchat(self, context: ReplyContext) -> AsyncGenerator[LlmStreamEvent]:
        call = len(self.chitchatted)
        self.chitchatted.append(context)
        outcome = self.chitchat_outcomes[min(call, len(self.chitchat_outcomes) - 1)]
        try:
            for chunk in self.chunks:
                yield TextChunk(chunk)
            output = "".join(self.chunks) if outcome is LlmCallOutcome.SUCCESS else None
            yield StreamEnd(
                LlmGatewayResult(
                    output=output,
                    friendly_error=None if output is not None else "failed",
                    outcome=outcome,
                )
            )
        finally:
            self.closed_streams += 1

    async def extract(self, context: ReplyContext, sport_id: str) -> Extraction:
        call = len(self.extracted)
        self.extracted.append((context, sport_id))
        outcome = self.extract_outcomes[min(call, len(self.extract_outcomes) - 1)]
        output = self.extractions[min(call, len(self.extractions) - 1)]
        return Extraction(
            output=output if outcome is LlmCallOutcome.SUCCESS else None,
            outcome=outcome,
            template=TemplateRef(id=f"{sport_id}.extract", version=1),
        )

    def draft_ready(self) -> FixedReply:
        return FixedReply(text=DRAFT_READY_TEXT, template=DRAFT_READY_TEMPLATE)

    def not_yet(self) -> FixedReply:
        return FixedReply(text=NOT_YET_TEXT, template=NOT_YET_TEMPLATE)


@dataclass
class ScriptedSafetyScreen:
    """Answers every message with `pain` after `delay` seconds. Records each message it saw,
    sets `started` once screening begins, and counts the screens cancelled before answering."""

    pain: bool = False
    delay: float = 0.0
    screened: list[str] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: int = 0

    async def finds_pain(self, user_id: UUID, message: str) -> bool:
        self.screened.append(message)
        self.started.set()
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return self.pain


@dataclass
class ScriptedFastRouter:
    """Answers every message with `intents`; `None`, the default, leaves it to the LLM router.
    `plausible_sports` is what Jev read from the message, `None` when it could not tell.
    Records each context it was given."""

    intents: list[Intent] | None = None
    plausible_sports: tuple[str, ...] | None = None
    routed: list[ReplyContext] = field(default_factory=list)

    async def route(self, context: ReplyContext) -> FastRoute:
        self.routed.append(context)
        return FastRoute(intents=self.intents, plausible_sports=self.plausible_sports)


def reply_services(
    chat: InMemoryChatRepository,
    *,
    drafts: InMemoryDraftRepository | None = None,
    profile: InMemoryProfile | None = None,
    conversation: ScriptedConversation | None = None,
    fast_router: ScriptedFastRouter | None = None,
    safety_screen: ScriptedSafetyScreen | None = None,
    history_turns: int = 10,
) -> ReplyServices:
    return ReplyServices(
        chat=chat,
        conversations=chat.conversations,
        drafts=drafts or InMemoryDraftRepository(),
        sports=sports_with_rowing(),
        profile=profile or InMemoryProfile(),
        conversation=conversation or ScriptedConversation(),
        fast_router=fast_router or ScriptedFastRouter(),
        safety_screen=safety_screen or ScriptedSafetyScreen(),
        clock=FixedClock(),
        history_turns=history_turns,
    )


def include_chat(
    app: FastAPI,
    templates: Jinja2Templates,
    chat: InMemoryChatRepository | None = None,
    *,
    conversation: ScriptedConversation | None = None,
    profile: InMemoryProfile | None = None,
    drafts: InMemoryDraftRepository | None = None,
) -> InMemoryChatRepository:
    """Mounts the chat router on an in-memory repository and a scripted conversation, and
    returns the repository."""
    repository = chat or InMemoryChatRepository()
    services = reply_services(repository, drafts=drafts, profile=profile, conversation=conversation)
    app.include_router(
        build_chat_router(templates, chat=repository, replies=services, sports=sports_with_rowing())
    )
    return repository


def sign_in_as(app: FastAPI, user: User | None = None) -> User:
    """Stands in for the active-user gate in a test app that doesn't wire it: every request
    carries `user` as `request.state.user`."""
    signed_in = user or User(
        id=uuid4(),
        sub="chat-test-sub",
        email="athlete@example.com",
        name="Ada Lovelace",
        locale="en",
        status=UserStatus.ACTIVE,
        created_at=FROZEN_NOW,
    )

    @app.middleware("http")
    async def _inject_user(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.user = signed_in
        return await call_next(request)

    return signed_in


async def pending_draft(
    drafts: InMemoryDraftRepository, user_id: UUID, *, status: DraftStatus = "pending"
) -> StoredDraft:
    """A test's shortcut: the athlete's climbing draft of `THREE_6A_TWO_6B`, in a conversation
    and from a log of its own, with `status`."""
    stored = await drafts.add(
        user_id,
        uuid4(),
        uuid4(),
        "climbing",
        SessionDraft(
            occurred_on=THREE_6A_TWO_6B.occurred_on,
            date_source=THREE_6A_TWO_6B.date_source,
            timezone="Europe/Warsaw",
            payload=THREE_6A_TWO_6B.to_payload().model_dump(mode="json"),
        ),
        payload_version=1,
        created_at=FROZEN_NOW,
    )
    if status != "pending":
        drafts.set_status(stored.id, status)
    return next(draft for draft in drafts.all_of(user_id) if draft.id == stored.id)
