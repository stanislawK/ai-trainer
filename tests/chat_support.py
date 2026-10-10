"""Fakes for the chat (tickets #78 and #82): an in-memory chat, a frozen clock, an athlete's
profile and a scripted router and chitchat specialist. Tests that mount the chat router only
because `/` is the signed-in home page use them too."""

from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import takewhile
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from starlette.templating import Jinja2Templates

from ai_trainer.application.ports.conversation import (
    FixedReply,
    ReplyContext,
    Routing,
    TemplateRef,
)
from ai_trainer.application.ports.llm_gateway import (
    LlmGatewayResult,
    LlmStreamEvent,
    StreamEnd,
    TextChunk,
)
from ai_trainer.application.reply import ReplyServices
from ai_trainer.domain.chat import ChatMessage, ChatRole
from ai_trainer.domain.conversation import Intent
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.users import User, UserStatus
from ai_trainer.web.chat import build_chat_router

FROZEN_NOW = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class InMemoryChatRepository:
    def __init__(self) -> None:
        self.rows: list[tuple[UUID, ChatMessage]] = []

    async def add(
        self,
        user_id: UUID,
        role: ChatRole,
        text: str,
        created_at: datetime,
        *,
        sports: Sequence[str] = (),
        template_id: str | None = None,
        template_version: int | None = None,
    ) -> ChatMessage:
        message = ChatMessage(
            id=uuid4(),
            role=role,
            text=text,
            created_at=created_at,
            sports=tuple(sports),
            template_id=template_id,
            template_version=template_version,
        )
        self.rows.append((user_id, message))
        return message

    def _own(self, user_id: UUID) -> list[ChatMessage]:
        """Ordered by `created_at` like the real repository; ties keep insertion order."""
        own = [message for owner, message in self.rows if owner == user_id]
        return sorted(own, key=lambda message: message.created_at)

    async def get(self, user_id: UUID, message_id: UUID) -> ChatMessage | None:
        return next((message for message in self._own(user_id) if message.id == message_id), None)

    async def list_recent(self, user_id: UUID, limit: int) -> Sequence[ChatMessage]:
        return self._own(user_id)[-limit:]

    async def list_before(
        self, user_id: UUID, before: ChatMessage, limit: int
    ) -> Sequence[ChatMessage]:
        own = self._own(user_id)
        return own[: own.index(before)][-limit:]

    async def list_replies(self, user_id: UUID, to: ChatMessage) -> Sequence[ChatMessage]:
        own = self._own(user_id)
        later = own[own.index(to) + 1 :]
        return list(takewhile(lambda message: message.role is ChatRole.ASSISTANT, later))


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


NOT_YET_TEXT = "I can't do that one yet."
CHITCHAT_TEMPLATE = TemplateRef(id="chitchat", version=1)
NOT_YET_TEMPLATE = TemplateRef(id="not_yet", version=1)


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
    routed: list[ReplyContext] = field(default_factory=list)
    chitchatted: list[ReplyContext] = field(default_factory=list)
    closed_streams: int = 0

    @property
    def chitchat_template(self) -> TemplateRef:
        return CHITCHAT_TEMPLATE

    async def route(self, context: ReplyContext) -> Routing:
        call = len(self.routed)
        self.routed.append(context)
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

    def not_yet(self) -> FixedReply:
        return FixedReply(text=NOT_YET_TEXT, template=NOT_YET_TEMPLATE)


def reply_services(
    chat: InMemoryChatRepository,
    *,
    profile: InMemoryProfile | None = None,
    conversation: ScriptedConversation | None = None,
    history_turns: int = 10,
) -> ReplyServices:
    return ReplyServices(
        chat=chat,
        profile=profile or InMemoryProfile(),
        conversation=conversation or ScriptedConversation(),
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
) -> InMemoryChatRepository:
    """Mounts the chat router on an in-memory repository and a scripted conversation, and
    returns the repository."""
    repository = chat or InMemoryChatRepository()
    services = reply_services(repository, profile=profile, conversation=conversation)
    app.include_router(
        build_chat_router(
            templates, chat=repository, replies=services, sports=default_sport_registry()
        )
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
