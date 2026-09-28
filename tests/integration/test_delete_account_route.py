"""Integration tests for self-service account deletion (G7, ADR-0004, ADR-0005, ticket #17)
through the real middleware stack and PostgreSQL: the deleted user is signed out immediately,
no row in any user-owned table survives, and another user's data is untouched."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.llm_calls_repository import SqlAlchemyLlmCallsRepository
from ai_trainer.adapters.orm import SessionOrm, UserStatusChangeOrm
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.llm_calls import LlmCallOutcome, NewLlmCall
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CSRF_HEADER_NAME, CsrfMiddleware
from ai_trainer.web.settings import build_settings_router
from ai_trainer.web.templating import build_templates

FROZEN_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
SECRET = b"integration-test-secret"

_CSRF_TOKEN_IN_HTML = re.compile(r'hx-headers:inherited=\'\{"X-CSRF-Token": "([0-9a-f]+)"\}\'')


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


def _client(
    *, users: SqlAlchemyUsersRepository, sessions: SqlAlchemySessionsRepository
) -> TestClient:
    app = FastAPI()
    templates = build_templates()
    app.include_router(build_settings_router(templates, users=users))
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
        templates=templates,
        admin_emails=[],
    )
    app.add_middleware(CsrfMiddleware, secret_key=SECRET, templates=templates)
    return TestClient(app)


async def _create_active_user(users: SqlAlchemyUsersRepository, *, sub: str) -> User:
    return await users.create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=sub, locale="en", status=UserStatus.ACTIVE
        )
    )


async def _create_session(sessions: SqlAlchemySessionsRepository, user: User) -> UUID:
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    return session.id


def _csrf_token(client: TestClient) -> str:
    """The token lives in `layouts/base.html`'s `<html>` tag, so a full-page GET is required --
    an `HX-Request` partial doesn't render it."""
    page = client.get("/settings")
    match = _CSRF_TOKEN_IN_HTML.search(page.text)
    assert match is not None, page.text
    return match.group(1)


async def test_confirming_deletion_ends_session_and_next_request_is_unauthenticated(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await _create_active_user(users, sub="delete-me-1")
    session_id = await _create_session(sessions, user)
    client = _client(users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    token = _csrf_token(client)

    response = client.post(
        "/settings/delete-account", headers={CSRF_HEADER_NAME: token, "HX-Request": "true"}
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/"
    assert await users.get(user.id) is None
    assert await sessions.get(session_id) is None

    # A fresh request carrying the very same (now-dead) session cookie is unauthenticated,
    # not merely logged out client-side.
    other_client = _client(users=users, sessions=sessions)
    other_client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    assert other_client.get("/settings").status_code == 401


_INSERT_STATUS_CHANGE = text(
    "INSERT INTO user_status_changes "
    "(id, actor_user_id, target_user_id, old_status, new_status) "
    "VALUES (:id, :actor_user_id, :target_user_id, 'pending', 'active')"
)


async def test_no_user_owned_row_references_the_deleted_user_id(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    llm_calls = SqlAlchemyLlmCallsRepository(db_session_factory)
    admin = await _create_active_user(users, sub="delete-me-audit-actor")
    target = await _create_active_user(users, sub="delete-me-audit-target")
    session_id = await _create_session(sessions, admin)
    # `admin` appears as both the actor and the target of an audit row (inserted directly so
    # the live status column isn't disturbed by going through the status-change service), and
    # has an `llm_calls` row of their own -- every user-owned table gets a reference.
    async with db_session_factory() as db:
        await db.execute(
            _INSERT_STATUS_CHANGE,
            {"id": uuid4(), "actor_user_id": admin.id, "target_user_id": target.id},
        )
        await db.execute(
            _INSERT_STATUS_CHANGE,
            {"id": uuid4(), "actor_user_id": target.id, "target_user_id": admin.id},
        )
        await db.commit()
    await llm_calls.record(
        NewLlmCall(
            user_id=admin.id,
            template_id="greeter",
            template_version=1,
            model="openai/gpt-5-mini",
            input_tokens=10,
            output_tokens=5,
            cost=None,
            latency_ms=42,
            outcome=LlmCallOutcome.SUCCESS,
        )
    )
    client = _client(users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    token = _csrf_token(client)

    response = client.post(
        "/settings/delete-account", headers={CSRF_HEADER_NAME: token, "HX-Request": "true"}
    )

    assert response.status_code == 200
    async with db_session_factory() as db:
        session_rows = (
            await db.scalars(select(SessionOrm).where(SessionOrm.user_id == admin.id))
        ).all()
        status_change_rows = (
            await db.scalars(
                select(UserStatusChangeOrm).where(
                    (UserStatusChangeOrm.actor_user_id == admin.id)
                    | (UserStatusChangeOrm.target_user_id == admin.id)
                )
            )
        ).all()
    assert session_rows == []
    assert status_change_rows == []
    assert await llm_calls.list_for_user(admin.id) == []


async def test_deleting_one_user_leaves_another_users_rows_intact(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    victim = await _create_active_user(users, sub="delete-me-tenancy-victim")
    other = await _create_active_user(users, sub="delete-me-tenancy-other")
    victim_session_id = await _create_session(sessions, victim)
    other_session_id = await _create_session(sessions, other)
    client = _client(users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(victim_session_id))
    token = _csrf_token(client)

    response = client.post(
        "/settings/delete-account", headers={CSRF_HEADER_NAME: token, "HX-Request": "true"}
    )

    assert response.status_code == 200
    assert await users.get(other.id) is not None
    assert await sessions.get(other_session_id) is not None
