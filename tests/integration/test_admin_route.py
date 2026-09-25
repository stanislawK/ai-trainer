"""Integration tests for the admin user-management page through the real middleware stack
and PostgreSQL (F7, ADR-0005): CSRF, tenancy (non-admin refused), immediate session
revocation, and the actor/target/old/new/timestamp audit trail."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import UserStatusChangeOrm
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.user_status_changer import SqlAlchemyUserStatusChanger
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.admin import build_admin_router
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CSRF_HEADER_NAME, CsrfMiddleware
from ai_trainer.web.templating import build_templates

FROZEN_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
SECRET = b"integration-test-secret"

_CSRF_TOKEN_IN_HTML = re.compile(r'hx-headers:inherited=\'\{"X-CSRF-Token": "([0-9a-f]+)"\}\'')


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


def _client(
    *,
    users: SqlAlchemyUsersRepository,
    sessions: SqlAlchemySessionsRepository,
    status_changer: SqlAlchemyUserStatusChanger,
    admin_emails: list[str],
) -> TestClient:
    app = FastAPI()
    templates = build_templates()
    app.include_router(
        build_admin_router(
            templates=templates, users=users, sessions=sessions, status_changer=status_changer
        )
    )
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
        templates=templates,
        admin_emails=admin_emails,
    )
    app.add_middleware(CsrfMiddleware, secret_key=SECRET, templates=templates)
    return TestClient(app)


async def _create_active_user(
    users: SqlAlchemyUsersRepository, *, sub: str, status: UserStatus = UserStatus.ACTIVE
) -> User:
    return await users.create(
        NewUser(sub=sub, email=f"{sub}@example.com", name=sub, locale="en", status=status)
    )


async def _create_session(sessions: SqlAlchemySessionsRepository, user: User) -> UUID:
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    return session.id


def _csrf_token(client: TestClient) -> str:
    """The token lives in `layouts/base.html`'s `<html>` tag, so a full-page GET is required —
    an `HX-Request` partial doesn't render it."""
    page = client.get("/admin")
    match = _CSRF_TOKEN_IN_HTML.search(page.text)
    assert match is not None, page.text
    return match.group(1)


async def test_admin_sees_every_user_with_their_current_status(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    status_changer = SqlAlchemyUserStatusChanger(db_session_factory)
    admin = await _create_active_user(users, sub="admin-list-1")
    pending = await _create_active_user(users, sub="pending-list-1", status=UserStatus.PENDING)
    session_id = await _create_session(sessions, admin)
    client = _client(
        users=users, sessions=sessions, status_changer=status_changer, admin_emails=[admin.email]
    )
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))

    response = client.get("/admin")

    assert response.status_code == 200
    assert admin.email in response.text
    assert pending.email in response.text


async def test_non_admin_gets_403_from_get_and_post(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    status_changer = SqlAlchemyUserStatusChanger(db_session_factory)
    non_admin = await _create_active_user(users, sub="non-admin-1")
    target = await _create_active_user(users, sub="target-non-admin-1", status=UserStatus.PENDING)
    session_id = await _create_session(sessions, non_admin)
    client = _client(users=users, sessions=sessions, status_changer=status_changer, admin_emails=[])
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))

    get_response = client.get("/admin")
    assert get_response.status_code == 403

    post_response = client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "active"},
        headers={CSRF_HEADER_NAME: "irrelevant", "HX-Request": "true"},
    )
    assert post_response.status_code == 403


async def test_post_without_csrf_token_is_rejected(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    status_changer = SqlAlchemyUserStatusChanger(db_session_factory)
    admin = await _create_active_user(users, sub="admin-csrf-1")
    target = await _create_active_user(users, sub="target-csrf-1", status=UserStatus.PENDING)
    session_id = await _create_session(sessions, admin)
    client = _client(
        users=users, sessions=sessions, status_changer=status_changer, admin_emails=[admin.email]
    )
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))

    response = client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "active"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 403
    found = await users.get(target.id)
    assert found is not None
    assert found.status is UserStatus.PENDING


async def test_non_admin_post_with_a_malformed_status_still_gets_403_not_422(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """The admin gate must run before a typed `status`/`user_id` parameter would otherwise be
    validated by FastAPI, which would let a non-admin trigger a 422 instead of a clean 403 just
    by sending a bogus value (ticket #16 review-gate skeptic finding)."""
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    status_changer = SqlAlchemyUserStatusChanger(db_session_factory)
    non_admin = await _create_active_user(users, sub="non-admin-malformed-1")
    target = await _create_active_user(
        users, sub="target-non-admin-malformed-1", status=UserStatus.PENDING
    )
    session_id = await _create_session(sessions, non_admin)
    client = _client(users=users, sessions=sessions, status_changer=status_changer, admin_emails=[])
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    token = _csrf_token(client)

    response = client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "not-a-real-status"},
        headers={CSRF_HEADER_NAME: token, "HX-Request": "true"},
    )

    assert response.status_code == 403


async def test_activating_a_pending_user_lets_them_reach_the_app(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    status_changer = SqlAlchemyUserStatusChanger(db_session_factory)
    admin = await _create_active_user(users, sub="admin-activate-1")
    target = await _create_active_user(users, sub="target-activate-1", status=UserStatus.PENDING)
    admin_session_id = await _create_session(sessions, admin)
    client = _client(
        users=users, sessions=sessions, status_changer=status_changer, admin_emails=[admin.email]
    )
    client.cookies.set(SESSION_COOKIE_NAME, str(admin_session_id))
    token = _csrf_token(client)

    response = client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "active"},
        headers={CSRF_HEADER_NAME: token, "HX-Request": "true"},
    )

    assert response.status_code == 200
    updated = await users.get(target.id)
    assert updated is not None
    assert updated.status is UserStatus.ACTIVE

    # The now-active target reaches a real route on their own next request.
    target_session_id = await _create_session(sessions, updated)
    target_client = _client(
        users=users, sessions=sessions, status_changer=status_changer, admin_emails=[admin.email]
    )
    target_client.cookies.set(SESSION_COOKIE_NAME, str(target_session_id))
    assert target_client.get("/admin").status_code == 403  # reaches the app, just not as admin


async def test_disabling_an_active_user_ends_their_live_session_immediately(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    status_changer = SqlAlchemyUserStatusChanger(db_session_factory)
    admin = await _create_active_user(users, sub="admin-disable-1")
    target = await _create_active_user(users, sub="target-disable-1", status=UserStatus.ACTIVE)
    admin_session_id = await _create_session(sessions, admin)
    target_session_id = await _create_session(sessions, target)
    client = _client(
        users=users, sessions=sessions, status_changer=status_changer, admin_emails=[admin.email]
    )
    client.cookies.set(SESSION_COOKIE_NAME, str(admin_session_id))
    token = _csrf_token(client)

    response = client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "disabled"},
        headers={CSRF_HEADER_NAME: token, "HX-Request": "true"},
    )

    assert response.status_code == 200
    assert await sessions.get(target_session_id) is None


async def test_admin_cannot_change_their_own_status(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    status_changer = SqlAlchemyUserStatusChanger(db_session_factory)
    admin = await _create_active_user(users, sub="admin-self-1")
    admin_session_id = await _create_session(sessions, admin)
    client = _client(
        users=users, sessions=sessions, status_changer=status_changer, admin_emails=[admin.email]
    )
    client.cookies.set(SESSION_COOKIE_NAME, str(admin_session_id))
    token = _csrf_token(client)

    response = client.post(
        f"/admin/users/{admin.id}/status",
        data={"status": "disabled"},
        headers={CSRF_HEADER_NAME: token, "HX-Request": "true"},
    )

    assert response.status_code == 400
    found = await users.get(admin.id)
    assert found is not None
    assert found.status is UserStatus.ACTIVE
    assert await sessions.get(admin_session_id) is not None


async def test_removing_an_admin_email_demotes_on_the_next_request(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    status_changer = SqlAlchemyUserStatusChanger(db_session_factory)
    user = await _create_active_user(users, sub="demoted-admin-1")
    session_id = await _create_session(sessions, user)

    admin_client = _client(
        users=users, sessions=sessions, status_changer=status_changer, admin_emails=[user.email]
    )
    admin_client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    assert admin_client.get("/admin").status_code == 200

    demoted_client = _client(
        users=users, sessions=sessions, status_changer=status_changer, admin_emails=[]
    )
    demoted_client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    assert demoted_client.get("/admin").status_code == 403


async def test_every_change_records_actor_target_old_new_and_timestamp(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    status_changer = SqlAlchemyUserStatusChanger(db_session_factory)
    admin = await _create_active_user(users, sub="admin-audit-1")
    target = await _create_active_user(users, sub="target-audit-1", status=UserStatus.PENDING)
    admin_session_id = await _create_session(sessions, admin)
    client = _client(
        users=users, sessions=sessions, status_changer=status_changer, admin_emails=[admin.email]
    )
    client.cookies.set(SESSION_COOKIE_NAME, str(admin_session_id))
    token = _csrf_token(client)

    client.post(
        f"/admin/users/{target.id}/status",
        data={"status": "active"},
        headers={CSRF_HEADER_NAME: token, "HX-Request": "true"},
    )

    async with db_session_factory() as session:
        rows = (
            await session.scalars(
                select(UserStatusChangeOrm).where(UserStatusChangeOrm.target_user_id == target.id)
            )
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_user_id == admin.id
    assert row.old_status == "pending"
    assert row.new_status == "active"
    assert row.created_at is not None
