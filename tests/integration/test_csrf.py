import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.users import GoogleClaims, NewUser, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.auth import SESSION_COOKIE_NAME, build_auth_router
from ai_trainer.web.csrf import CSRF_HEADER_NAME, CsrfMiddleware
from ai_trainer.web.home import build_home_router
from ai_trainer.web.templating import build_templates

FROZEN_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
SECRET = b"integration-test-secret"

_CSRF_TOKEN_IN_HTML = re.compile(r'hx-headers:inherited=\'\{"X-CSRF-Token": "([0-9a-f]+)"\}\'')


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class _UnusedOAuthClient:
    """Satisfies `build_auth_router`'s dependency; these tests seed sessions directly and never
    exercise `/auth/login` or `/auth/callback`."""

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> RedirectResponse:
        raise AssertionError("not exercised by these tests")

    async def authorize_access_token(self, request: Request) -> GoogleClaims:
        raise AssertionError("not exercised by these tests")


def _client(
    session_factory: Callable[[], AsyncSession],
    *,
    users: SqlAlchemyUsersRepository,
    sessions: SqlAlchemySessionsRepository,
) -> TestClient:
    """Wires the home and auth routes behind both middlewares, mirroring `main.py`'s
    composition: `CsrfMiddleware` outermost so `request.state.csrf_token` is set before the
    gate or a route ever renders a template. The auth router is included so `/auth/logout` --
    exempt from the active-user gate but not from CSRF -- is exercised through the exact
    middleware stack production wires it behind, not in isolation (a gap a code review found:
    `tests/integration/test_auth_route.py` builds the auth router alone, with no CSRF
    middleware at all)."""
    app = FastAPI()
    templates = build_templates()
    app.include_router(build_home_router(templates))
    app.include_router(
        build_auth_router(
            oauth_client=_UnusedOAuthClient(),
            users=users,
            sessions=sessions,
            clock=FakeClock(),
            admin_emails=[],
            session_ttl=timedelta(days=14),
            cookie_secure=False,
        )
    )

    @app.post("/action")
    async def action() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
        templates=templates,
    )
    app.add_middleware(CsrfMiddleware, secret_key=SECRET, templates=templates)
    return TestClient(app)


async def _active_session(
    *,
    users: SqlAlchemyUsersRepository,
    sessions: SqlAlchemySessionsRepository,
    sub: str,
) -> UUID:
    user = await users.create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=sub, locale="en", status=UserStatus.ACTIVE
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    return session.id


async def test_a_post_carrying_a_csrf_token_matching_the_rendered_page_succeeds(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """Proves AC4: the token a rendered page exposes via `hx-headers:inherited` is exactly the
    one the server accepts back -- the same wiring an htmx-issued POST from that page relies on
    to carry the header automatically, with no per-form setup."""
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    session_id = await _active_session(users=users, sessions=sessions, sub="sub-csrf-1")
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))

    page = client.get("/")
    assert page.status_code == 200
    match = _CSRF_TOKEN_IN_HTML.search(page.text)
    assert match is not None, "rendered page must expose the CSRF token via hx-headers:inherited"
    token = match.group(1)

    response = client.post("/action", headers={CSRF_HEADER_NAME: token, "HX-Request": "true"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_a_post_with_no_csrf_token_is_rejected(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    session_id = await _active_session(users=users, sessions=sessions, sub="sub-csrf-2")
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))

    response = client.post("/action")

    assert response.status_code == 403


async def test_a_post_carrying_another_sessions_csrf_token_is_rejected(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    session_a = await _active_session(users=users, sessions=sessions, sub="sub-csrf-3a")
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_a))
    page = client.get("/")
    match = _CSRF_TOKEN_IN_HTML.search(page.text)
    assert match is not None
    session_a_token = match.group(1)

    session_b = await _active_session(users=users, sessions=sessions, sub="sub-csrf-3b")
    other_client = _client(db_session_factory, users=users, sessions=sessions)
    other_client.cookies.set(SESSION_COOKIE_NAME, str(session_b))
    other_page = other_client.get("/")
    other_match = _CSRF_TOKEN_IN_HTML.search(other_page.text)
    assert other_match is not None
    session_b_token = other_match.group(1)
    assert session_a_token != session_b_token

    response = client.post(
        "/action", headers={CSRF_HEADER_NAME: session_b_token, "HX-Request": "true"}
    )

    assert response.status_code == 403


async def test_logout_through_the_full_middleware_stack_succeeds_with_a_valid_token(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """`/auth/logout` is exempt from `ActiveUserGateMiddleware` (ADR-0005 invariant 2) but not
    from CSRF (invariant 3): this proves both hold at once, through the exact middleware
    composition `main.py` wires."""
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    session_id = await _active_session(users=users, sessions=sessions, sub="sub-csrf-logout-1")
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))
    page = client.get("/")
    match = _CSRF_TOKEN_IN_HTML.search(page.text)
    assert match is not None
    token = match.group(1)

    response = client.post(
        "/auth/logout", headers={CSRF_HEADER_NAME: token}, follow_redirects=False
    )

    assert response.status_code == 303
    assert await sessions.get(session_id) is None


async def test_logout_through_the_full_middleware_stack_is_rejected_with_no_token(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    session_id = await _active_session(users=users, sessions=sessions, sub="sub-csrf-logout-2")
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session_id))

    response = client.post("/auth/logout", follow_redirects=False)

    assert response.status_code == 403
    assert await sessions.get(session_id) is not None
