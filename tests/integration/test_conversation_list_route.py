"""The conversation list on PostgreSQL (PRD-0008 B25, B26, G1, ADR-0021, ticket #213): with a
frozen `Clock`, a conversation last active exactly `conversation_retention_days` ago is listed
and one a second older is not, and another athlete's conversations never show and never open."""

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.chat import build_chat_router
from ai_trainer.web.templating import build_templates
from tests.chat_support import (
    FROZEN_NOW,
    FixedClock,
    InMemoryChatRepository,
    reply_services,
    sign_in_as,
    sports_with_rowing,
)

_RETENTION_DAYS = 14


async def _user(factory: Callable[[], AsyncSession], sub: str) -> User:
    return await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )


def _conversations(factory: Callable[[], AsyncSession]) -> SqlAlchemyConversationRepository:
    return SqlAlchemyConversationRepository(
        factory, clock=FixedClock(), retention_days=_RETENTION_DAYS
    )


def _client(factory: Callable[[], AsyncSession], user: User) -> TestClient:
    chat = SqlAlchemyChatRepository(factory)
    replies = replace(
        reply_services(InMemoryChatRepository()), chat=chat, conversations=_conversations(factory)
    )
    app = FastAPI()
    app.include_router(
        build_chat_router(
            build_templates(),
            chat=chat,
            replies=replies,
            sports=sports_with_rowing(),
            retention_days=_RETENTION_DAYS,
        )
    )
    sign_in_as(app, user)
    return TestClient(app)


async def _conversation(
    factory: Callable[[], AsyncSession], user_id: UUID, title: str, age: timedelta
) -> UUID:
    when = FROZEN_NOW - age
    conversation = await _conversations(factory).create(user_id, title, when)
    await SqlAlchemyChatRepository(factory).add(
        user_id, conversation.id, ChatRole.USER, title, when
    )
    return conversation.id


def _listed(html: str) -> list[str]:
    (phone,) = re.findall(
        r'<div id="conversation-list-phone"[^>]*>.*?<!-- /conversation-list -->', html, re.S
    )
    return re.findall(r'data-conversation-row="([^"]+)"', phone)


async def test_the_list_keeps_day_fourteen_and_drops_a_second_past_it(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _user(db_session_factory, "list-boundary")
    kept = await _conversation(
        db_session_factory, athlete.id, "Day fourteen", timedelta(days=_RETENTION_DAYS)
    )
    gone = await _conversation(
        db_session_factory, athlete.id, "Past it", timedelta(days=_RETENTION_DAYS, seconds=1)
    )
    client = _client(db_session_factory, athlete)

    response = client.get("/c/new")

    assert _listed(response.text) == [str(kept)]
    assert client.get(f"/c/{gone}").status_code == 404


async def test_another_athletes_conversations_are_never_listed_or_opened(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete_a = await _user(db_session_factory, "list-a")
    athlete_b = await _user(db_session_factory, "list-b")
    own = await _conversation(db_session_factory, athlete_a.id, "Mine", timedelta(hours=2))
    theirs = await _conversation(db_session_factory, athlete_b.id, "Theirs", timedelta(hours=1))
    client = _client(db_session_factory, athlete_a)

    response = client.get("/")

    assert _listed(response.text) == [str(own)]
    assert str(theirs) not in response.text
    assert "Theirs" not in response.text
    assert client.get(f"/c/{theirs}").status_code == 404
    assert client.get(f"/c/{theirs}", headers={"HX-Request": "true"}).status_code == 404
