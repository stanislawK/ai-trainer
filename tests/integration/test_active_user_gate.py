from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.onboarding_repository import SqlAlchemyOnboardingRepository
from ai_trainer.adapters.orm import UserOrm
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.user_status_changer import SqlAlchemyUserStatusChanger
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.users import GoogleClaims, NewUser, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.auth import SESSION_COOKIE_NAME, build_auth_router
from ai_trainer.web.health import build_health_router
from ai_trainer.web.sign_in import build_sign_in_router
from ai_trainer.web.templating import build_templates
from tests.chat_support import include_chat

# Far in the future: the session cookie carries `expires=FROZEN_NOW + ttl`, and the test client
# drops a cookie that is already past against the real clock.
FROZEN_NOW = datetime(2099, 9, 22, 12, 0, tzinfo=UTC)


async def _mark_onboarded(session_factory: Callable[[], AsyncSession], user_id: UUID) -> None:
    """These tests are about the gate's status handling, not onboarding: they need users who
    have already finished it, or `/` would redirect to the sports step (ticket #74)."""
    await SqlAlchemyOnboardingRepository(session_factory).set_onboarded_at(user_id, FROZEN_NOW)


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

    def __init__(self, *, email: str = "other@example.com", sub: str = "google-sub-other") -> None:
        self._email = email
        self._sub = sub

    async def authorize_access_token(self, request: Request) -> GoogleClaims:
        return GoogleClaims(
            sub=self._sub, email=self._email, email_verified=True, name="Other", locale="en"
        )


def _client(
    session_factory: Callable[[], AsyncSession],
    *,
    users: SqlAlchemyUsersRepository,
    sessions: SqlAlchemySessionsRepository,
    include_auth_router: bool = False,
    admin_emails: list[str] | None = None,
    oauth_client: FakeGoogleOAuthClient | None = None,
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
    include_chat(app, templates)
    app.include_router(
        build_sign_in_router(templates=templates, users=users, sessions=sessions, clock=FakeClock())
    )
    if include_auth_router:
        app.include_router(
            build_auth_router(
                oauth_client=oauth_client or FakeGoogleOAuthClient(),
                users=users,
                sessions=sessions,
                status_changer=SqlAlchemyUserStatusChanger(session_factory),
                clock=FakeClock(),
                admin_emails=admin_emails or [],
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
    assert "Tell me what you trained" not in response.text
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
        row.onboarded_at = FROZEN_NOW
        await write_session.commit()

    allowed = client.get("/")

    assert allowed.status_code == 200
    assert "Tell me what you trained" in allowed.text


async def test_disabling_an_active_user_mid_session_blocks_their_next_request(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(sub="sub-3", email="c@example.com", name="C", locale="en", status=UserStatus.ACTIVE)
    )
    await _mark_onboarded(db_session_factory, user.id)
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    allowed = client.get("/")
    assert allowed.status_code == 200
    assert "Tell me what you trained" in allowed.text

    async with db_session_factory() as write_session:
        row = await write_session.get(UserOrm, user.id)
        assert row is not None
        row.status = UserStatus.DISABLED.value
        await write_session.commit()

    blocked = client.get("/")

    assert blocked.status_code == 200
    assert "Tell me what you trained" not in blocked.text
    assert "This account is paused" in blocked.text


async def test_an_anonymous_visit_to_the_home_route_redirects_to_sign_in(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    client = _client(db_session_factory, users=users, sessions=sessions)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


async def test_an_anonymous_htmx_request_to_the_home_route_gets_an_hx_redirect_to_sign_in(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    client = _client(db_session_factory, users=users, sessions=sessions)

    response = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)

    assert response.status_code == 200
    assert response.headers["hx-redirect"] == "/sign-in"


async def test_a_revoked_session_cookie_on_the_home_route_still_gets_the_401_page(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(sub="sub-7", email="g@example.com", name="G", locale="en", status=UserStatus.ACTIVE)
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    await sessions.delete(session.id)
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 401


async def test_an_expired_session_cookie_on_the_home_route_still_gets_the_401_page(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(sub="sub-8", email="h@example.com", name="H", locale="en", status=UserStatus.ACTIVE)
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW - timedelta(minutes=1))
    )
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 401


async def test_a_malformed_session_cookie_on_the_home_route_still_gets_the_401_page(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, "not-a-uuid")

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 401


async def test_an_empty_session_cookie_on_the_home_route_is_treated_as_anonymous(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, "")

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"


async def test_an_anonymous_request_to_another_gated_route_still_gets_the_401_page(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    client = _client(db_session_factory, users=users, sessions=sessions)

    response = client.get("/settings", follow_redirects=False)

    assert response.status_code == 401
    assert "sign-in" in response.text


async def test_sign_in_never_redirects_to_itself_for_an_anonymous_visitor(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    client = _client(db_session_factory, users=users, sessions=sessions)

    response = client.get("/sign-in", follow_redirects=False)

    assert response.status_code == 200
    assert "location" not in response.headers
    assert "hx-redirect" not in response.headers


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


async def _sign_in_then_get_home(
    db_session_factory: Callable[[], AsyncSession],
    *,
    status: UserStatus,
    admin_emails: list[str],
) -> str:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    created = await users.create(
        NewUser(sub="sub-3", email="c@example.com", name="C", locale="en", status=status)
    )
    await _mark_onboarded(db_session_factory, created.id)
    client = _client(
        db_session_factory,
        users=users,
        sessions=sessions,
        include_auth_router=True,
        admin_emails=admin_emails,
        oauth_client=FakeGoogleOAuthClient(email="c@example.com", sub="sub-3"),
    )
    callback = client.get("/auth/callback", follow_redirects=False)
    assert callback.headers["location"] == "/"
    response = client.get("/")
    assert response.status_code == 200
    return response.text


async def test_a_listed_pending_account_signs_in_and_reaches_home(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    text = await _sign_in_then_get_home(
        db_session_factory, status=UserStatus.PENDING, admin_emails=["C@Example.com"]
    )

    assert "Tell me what you trained" in text
    assert "on the list" not in text


async def test_an_unlisted_pending_account_signs_in_and_sees_the_status_screen(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    text = await _sign_in_then_get_home(
        db_session_factory, status=UserStatus.PENDING, admin_emails=["someone-else@example.com"]
    )

    assert "Tell me what you trained" not in text
    assert "on the list" in text
