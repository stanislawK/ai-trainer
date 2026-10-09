"""Fakes for tests that mount the chat router (ticket #78) only because `/` is the signed-in
home page: an in-memory chat and a frozen clock."""

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from starlette.templating import Jinja2Templates

from ai_trainer.domain.chat import ChatMessage, ChatRole
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
        self, user_id: UUID, role: ChatRole, text: str, created_at: datetime
    ) -> ChatMessage:
        message = ChatMessage(id=uuid4(), role=role, text=text, created_at=created_at)
        self.rows.append((user_id, message))
        return message

    async def list_recent(self, user_id: UUID, limit: int) -> Sequence[ChatMessage]:
        return [message for owner, message in self.rows if owner == user_id][-limit:]


def include_chat(
    app: FastAPI, templates: Jinja2Templates, chat: InMemoryChatRepository | None = None
) -> InMemoryChatRepository:
    """Mounts the chat router on an in-memory repository and returns it."""
    repository = chat or InMemoryChatRepository()
    app.include_router(build_chat_router(templates, chat=repository, clock=FixedClock()))
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
