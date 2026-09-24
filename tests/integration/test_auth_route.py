from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from authlib.integrations.base_client import MismatchingStateError
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.orm import UserOrm
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.users import GoogleClaims, UserStatus
from ai_trainer.web.auth import build_auth_router

FROZEN_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
SESSION_TTL = timedelta(days=14)


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class FakeGoogleOAuthClient:
    """Never touches the network: stands in for Authlib in every route test (ADR-0013)."""

    def __init__(
        self, *, claims: GoogleClaims | None = None, error: Exception | None = None
    ) -> None:
        self._claims = claims
        self._error = error

    async def authorize_redirect(self, request: Request, redirect_uri: str) -> RedirectResponse:
        return RedirectResponse(
            url=f"https://accounts.google.com/o/oauth2/auth?redirect_uri={redirect_uri}"
        )

    async def authorize_access_token(self, request: Request) -> GoogleClaims:
        if self._error is not None:
            raise self._error
        assert self._claims is not None
        return self._claims


def _claims(**overrides: object) -> GoogleClaims:
    defaults: dict[str, object] = {
        "sub": "google-sub-1",
        "email": "athlete@example.com",
        "email_verified": True,
        "name": "Athlete",
        "locale": "en",
    }
    defaults.update(overrides)
    return GoogleClaims.model_validate(defaults)


def _client(
    session_factory: Callable[[], AsyncSession],
    *,
    oauth_client: FakeGoogleOAuthClient,
    admin_emails: list[str] | None = None,
    cookie_secure: bool = True,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_auth_router(
            oauth_client=oauth_client,
            users=SqlAlchemyUsersRepository(session_factory),
            sessions=SqlAlchemySessionsRepository(session_factory),
            clock=FakeClock(),
            admin_emails=admin_emails or [],
            session_ttl=SESSION_TTL,
            cookie_secure=cookie_secure,
        )
    )
    return TestClient(app)


async def _count_users(session_factory: Callable[[], AsyncSession]) -> int:
    async with session_factory() as session:
        rows = await session.scalars(select(UserOrm))
        return len(list(rows))


async def test_login_redirects_through_the_oauth_client(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    client = _client(db_session_factory, oauth_client=FakeGoogleOAuthClient(claims=_claims()))

    response = client.get("/auth/login", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert "accounts.google.com" in response.headers["location"]


async def test_callback_creates_pending_user_session_and_cookie(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    client = _client(db_session_factory, oauth_client=FakeGoogleOAuthClient(claims=_claims()))

    response = client.get("/auth/callback", follow_redirects=False)

    assert response.status_code == 303
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "Secure" in set_cookie
    session_id = response.cookies.get("session_id")
    assert session_id is not None

    users = SqlAlchemyUsersRepository(db_session_factory)
    user = await users.get_by_sub("google-sub-1")
    assert user is not None
    assert user.status is UserStatus.PENDING

    sessions = SqlAlchemySessionsRepository(db_session_factory)
    session = await sessions.get(UUID(session_id))
    assert session is not None
    assert session.user_id == user.id
    assert session.expires_at == FROZEN_NOW + SESSION_TTL


async def test_callback_with_verified_admin_email_creates_active_user(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    client = _client(
        db_session_factory,
        oauth_client=FakeGoogleOAuthClient(claims=_claims(email="admin@example.com")),
        admin_emails=["Admin@Example.com"],
    )

    client.get("/auth/callback", follow_redirects=False)

    users = SqlAlchemyUsersRepository(db_session_factory)
    user = await users.get_by_sub("google-sub-1")
    assert user is not None
    assert user.status is UserStatus.ACTIVE


async def test_second_callback_with_the_same_sub_reuses_the_user(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    client = _client(db_session_factory, oauth_client=FakeGoogleOAuthClient(claims=_claims()))

    client.get("/auth/callback", follow_redirects=False)
    client.get("/auth/callback", follow_redirects=False)

    assert await _count_users(db_session_factory) == 1


async def test_callback_with_mismatched_state_is_rejected_and_creates_no_user(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    client = _client(
        db_session_factory,
        oauth_client=FakeGoogleOAuthClient(error=MismatchingStateError()),
    )

    response = client.get("/auth/callback", follow_redirects=False)

    assert response.status_code == 400
    assert await _count_users(db_session_factory) == 0


async def test_logout_deletes_the_session_and_the_cookie_stops_working(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    # cookie_secure=False so the test client's cookie jar carries the cookie back over the
    # plain-http `testserver` connection between these two requests; production always sets
    # `Secure` (verified separately, over https, by the cookie-flags assertions above).
    client = _client(
        db_session_factory,
        oauth_client=FakeGoogleOAuthClient(claims=_claims()),
        cookie_secure=False,
    )
    callback_response = client.get("/auth/callback", follow_redirects=False)
    session_id = callback_response.cookies.get("session_id")
    assert session_id is not None
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    assert await sessions.get(UUID(session_id)) is not None

    logout_response = client.post("/auth/logout", follow_redirects=False)

    assert logout_response.status_code == 303
    assert await sessions.get(UUID(session_id)) is None
    set_cookie = logout_response.headers["set-cookie"]
    assert 'session_id=""' in set_cookie or "session_id=;" in set_cookie


def _unreachable_session_factory() -> AsyncSession:
    raise AssertionError("no cookie means no repository call")


async def test_logout_with_no_cookie_is_a_noop() -> None:
    client = _client(_unreachable_session_factory, oauth_client=FakeGoogleOAuthClient())

    response = client.post("/auth/logout", follow_redirects=False)

    assert response.status_code == 303


async def test_logout_with_a_malformed_cookie_is_a_noop_not_a_crash() -> None:
    client = _client(_unreachable_session_factory, oauth_client=FakeGoogleOAuthClient())
    client.cookies.set("session_id", "not-a-uuid")

    response = client.post("/auth/logout", follow_redirects=False)

    assert response.status_code == 303


async def test_htmx_logout_gets_a_200_with_hx_redirect_instead_of_a_3xx() -> None:
    """htmx never processes response headers on a 3xx status (`.claude/skills/apply-ticket`'s
    `implement-design` groundwork, ticket #40): the sidebar's sign-out button is the first UI
    caller of this route, and it goes through htmx to carry the CSRF header, so logout must
    answer it with `HX-Redirect` on a 2xx instead of the plain 303 non-htmx callers still get."""
    client = _client(_unreachable_session_factory, oauth_client=FakeGoogleOAuthClient())

    response = client.post("/auth/logout", headers={"HX-Request": "true"}, follow_redirects=False)

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/"
