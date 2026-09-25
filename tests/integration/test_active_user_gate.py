from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import UserOrm
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.users import GoogleClaims, NewUser, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.auth import SESSION_COOKIE_NAME, build_auth_router
from ai_trainer.web.health import build_health_router
from ai_trainer.web.home import build_home_router
from ai_trainer.web.sign_in import build_sign_in_router
from ai_trainer.web.templating import build_templates

FROZEN_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


class _NoOpHealth:
    async def ping(self) -> bool:
        return True


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class FakeGoogleOAuthClient:
    """Never touches the network, mirroring `test_auth_route.py` (ADR-0013)."""

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> RedirectResponse:
        return RedirectResponse(
            url=f"https://accounts.google.com/o/oauth2/auth?redirect_uri={redirect_uri}"
        )

    async def authorize_access_token(self, request: Request) -> GoogleClaims:
        return GoogleClaims(
            sub="google-sub-other",
            email="other@example.com",
            email_verified=True,
            name="Other",
            locale="en",
        )


def _client(
    session_factory: Callable[[], AsyncSession],
    *,
    users: SqlAlchemyUsersRepository,
    sessions: SqlAlchemySessionsRepository,
    include_auth_router: bool = False,
    admin_emails: list[str] | None = None,
) -> TestClient:
    app = FastAPI()
    templates = build_templates()
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
        templates=templates,
        admin_emails=admin_emails or [],
    )
    app.include_router(build_health_router(_NoOpHealth()))
    app.include_router(build_home_router(templates))
    app.include_router(
        build_sign_in_router(templates=templates, users=users, sessions=sessions, clock=FakeClock())
    )
    if include_auth_router:
        app.include_router(
            build_auth_router(
                oauth_client=FakeGoogleOAuthClient(),
                users=users,
                sessions=sessions,
                clock=FakeClock(),
                admin_emails=[],
                session_ttl=timedelta(days=14),
                cookie_secure=False,
            )
        )
    return TestClient(app)


async def test_a_pending_user_requesting_the_home_route_lands_on_the_status_screen(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(
            sub="sub-1", email="a@example.com", name="A", locale="en", status=UserStatus.PENDING
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/")

    assert response.status_code == 200
    assert "AI Trainer</h1>" not in response.text
    assert "on the list" in response.text


async def test_activating_a_pending_user_lets_the_next_request_through_with_no_new_sign_in(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(
            sub="sub-2", email="b@example.com", name="B", locale="en", status=UserStatus.PENDING
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    blocked = client.get("/")
    assert blocked.status_code == 200
    assert "on the list" in blocked.text

    async with db_session_factory() as write_session:
        row = await write_session.get(UserOrm, user.id)
        assert row is not None
        row.status = UserStatus.ACTIVE.value
        await write_session.commit()

    allowed = client.get("/")

    assert allowed.status_code == 200
    assert "AI Trainer</h1>" in allowed.text


async def test_disabling_an_active_user_mid_session_blocks_their_next_request(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(sub="sub-3", email="c@example.com", name="C", locale="en", status=UserStatus.ACTIVE)
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    allowed = client.get("/")
    assert allowed.status_code == 200
    assert "AI Trainer</h1>" in allowed.text

    async with db_session_factory() as write_session:
        row = await write_session.get(UserOrm, user.id)
        assert row is not None
        row.status = UserStatus.DISABLED.value
        await write_session.commit()

    blocked = client.get("/")

    assert blocked.status_code == 200
    assert "AI Trainer</h1>" not in blocked.text
    assert "This account is paused" in blocked.text


async def test_an_unauthenticated_request_to_the_home_route_is_refused(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    client = _client(db_session_factory, users=users, sessions=sessions)

    response = client.get("/")

    assert response.status_code == 401


async def test_health_stays_reachable_for_a_pending_user_and_with_no_cookie_at_all(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(
            sub="sub-4", email="d@example.com", name="D", locale="en", status=UserStatus.PENDING
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    client = _client(db_session_factory, users=users, sessions=sessions)

    anonymous_health = client.get("/health")
    assert anonymous_health.status_code == 200

    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    pending_health = client.get("/health")
    assert pending_health.status_code == 200


async def test_sign_in_stays_reachable_for_a_pending_user_while_home_does_not(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """`/sign-in` is exempt from the gate (ADR-0005 invariant 2, ticket #43) the same way
    `/health` and auth's own routes are, while an ordinary gated route like `/` still isn't."""
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(
            sub="sub-6", email="f@example.com", name="F", locale="en", status=UserStatus.PENDING
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    sign_in = client.get("/sign-in")
    assert sign_in.status_code == 200
    assert "Continue with Google" in sign_in.text

    home = client.get("/")
    assert home.status_code == 200
    assert "on the list" in home.text


async def test_sign_in_stays_reachable_with_no_session_at_all(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    client = _client(db_session_factory, users=users, sessions=sessions)

    response = client.get("/sign-in")

    assert response.status_code == 200


async def test_login_callback_and_logout_stay_reachable_for_a_pending_user(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """Unlike `/health` (exercised above), these three routes are auth's own entry and exit
    points; #14's exemption list is only proven for AC5 once each of them is actually hit
    through the real auth router while a pending user's cookie is attached (a gap a skeptic
    review of this ticket found the earlier test suite left unclosed)."""
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(
            sub="sub-5", email="e@example.com", name="E", locale="en", status=UserStatus.PENDING
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    client = _client(db_session_factory, users=users, sessions=sessions, include_auth_router=True)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    login = client.get("/auth/login", follow_redirects=False)
    assert login.status_code in (302, 303, 307)
    assert "accounts.google.com" in login.headers["location"]

    callback = client.get("/auth/callback", follow_redirects=False)
    assert callback.status_code == 303

    logout = client.post("/auth/logout", follow_redirects=False)
    assert logout.status_code == 303
