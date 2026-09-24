from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.templating import build_templates

FROZEN_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class FakeUsersRepository:
    def __init__(self, *users: User) -> None:
        self._users = {user.id: user for user in users}

    async def get_by_sub(self, sub: str) -> User | None:
        raise NotImplementedError

    async def get(self, user_id: UUID) -> User | None:
        return self._users.get(user_id)

    async def create(self, new_user: NewUser) -> User:
        raise NotImplementedError


class FakeSessionsRepository:
    def __init__(self, *sessions: Session) -> None:
        self._sessions = {session.id: session for session in sessions}

    async def create(self, new_session: NewSession) -> Session:
        raise NotImplementedError

    async def get(self, session_id: UUID) -> Session | None:
        return self._sessions.get(session_id)

    async def delete(self, session_id: UUID) -> None:
        raise NotImplementedError


def _user(status: UserStatus) -> User:
    return User(
        id=uuid4(),
        sub="google-sub-1",
        email="athlete@example.com",
        name="Athlete",
        locale="en",
        status=status,
        created_at=FROZEN_NOW,
    )


def _session(user: User, *, expires_at: datetime | None = None) -> Session:
    return Session(
        id=uuid4(),
        user_id=user.id,
        expires_at=expires_at or FROZEN_NOW + timedelta(days=1),
        created_at=FROZEN_NOW,
    )


def _client(
    *,
    users: FakeUsersRepository,
    sessions: FakeSessionsRepository,
    admin_emails: list[str] | None = None,
) -> TestClient:
    app = FastAPI()

    @app.get("/protected")
    async def protected() -> PlainTextResponse:
        return PlainTextResponse("feature content")

    @app.get("/health")
    async def health() -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/is-admin")
    async def is_admin_probe(request: Request) -> PlainTextResponse:
        return PlainTextResponse(str(request.state.is_admin))

    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
        templates=build_templates(),
        admin_emails=admin_emails or [],
    )
    return TestClient(app)


def test_exempt_route_is_reachable_with_no_cookie_at_all() -> None:
    client = _client(users=FakeUsersRepository(), sessions=FakeSessionsRepository())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.text == "ok"


def test_exempt_route_with_a_trailing_slash_is_still_reachable() -> None:
    """FastAPI's own router redirects `/health/` to `/health` (307) once routing is reached —
    downstream of this middleware. Regression: the gate used to compare the raw path against
    the exempt set with no normalization, so this trailing-slash request never got the chance
    to reach that redirect and was refused (401) instead."""
    client = _client(users=FakeUsersRepository(), sessions=FakeSessionsRepository())

    redirect = client.get("/health/", follow_redirects=False)
    assert redirect.status_code == 307

    response = client.get("/health/")

    assert response.status_code == 200
    assert response.text == "ok"


def test_feature_route_with_no_cookie_is_refused() -> None:
    client = _client(users=FakeUsersRepository(), sessions=FakeSessionsRepository())

    response = client.get("/protected")

    assert response.status_code == 401
    assert "sign in" in response.text.lower()


def test_unauthorized_response_is_an_htmx_partial_for_an_hx_request() -> None:
    client = _client(users=FakeUsersRepository(), sessions=FakeSessionsRepository())

    response = client.get("/protected", headers={"HX-Request": "true"})

    assert response.status_code == 401
    assert "<html" not in response.text


def test_feature_route_with_a_malformed_cookie_is_refused() -> None:
    client = _client(users=FakeUsersRepository(), sessions=FakeSessionsRepository())
    client.cookies.set(SESSION_COOKIE_NAME, "not-a-uuid")

    response = client.get("/protected")

    assert response.status_code == 401


def test_feature_route_with_an_unknown_session_is_refused() -> None:
    client = _client(users=FakeUsersRepository(), sessions=FakeSessionsRepository())
    client.cookies.set(SESSION_COOKIE_NAME, str(uuid4()))

    response = client.get("/protected")

    assert response.status_code == 401


def test_feature_route_with_an_expired_session_is_refused() -> None:
    user = _user(UserStatus.ACTIVE)
    session = _session(user, expires_at=FROZEN_NOW - timedelta(seconds=1))
    client = _client(users=FakeUsersRepository(user), sessions=FakeSessionsRepository(session))
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/protected")

    assert response.status_code == 401


def test_active_user_reaches_the_feature_route() -> None:
    user = _user(UserStatus.ACTIVE)
    session = _session(user)
    client = _client(users=FakeUsersRepository(user), sessions=FakeSessionsRepository(session))
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/protected")

    assert response.status_code == 200
    assert response.text == "feature content"


def test_pending_user_gets_the_status_screen_instead_of_the_feature_route() -> None:
    user = _user(UserStatus.PENDING)
    session = _session(user)
    client = _client(users=FakeUsersRepository(user), sessions=FakeSessionsRepository(session))
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/protected")

    assert response.status_code == 200
    assert "feature content" not in response.text
    assert "pending" in response.text.lower()


def test_disabled_user_gets_the_status_screen_instead_of_the_feature_route() -> None:
    user = _user(UserStatus.DISABLED)
    session = _session(user)
    client = _client(users=FakeUsersRepository(user), sessions=FakeSessionsRepository(session))
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/protected")

    assert response.status_code == 200
    assert "feature content" not in response.text
    assert "disabled" in response.text.lower()


def test_pending_user_status_screen_names_no_other_account_or_admin_contact() -> None:
    user = _user(UserStatus.PENDING)
    session = _session(user)
    client = _client(users=FakeUsersRepository(user), sessions=FakeSessionsRepository(session))
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/protected")

    assert user.email not in response.text
    assert "admin" not in response.text.lower()


def test_active_user_whose_email_is_not_an_admin_email_has_is_admin_false() -> None:
    user = _user(UserStatus.ACTIVE)
    session = _session(user)
    client = _client(
        users=FakeUsersRepository(user), sessions=FakeSessionsRepository(session), admin_emails=[]
    )
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/is-admin")

    assert response.text == "False"


def test_active_user_whose_email_is_an_admin_email_has_is_admin_true() -> None:
    user = _user(UserStatus.ACTIVE)
    session = _session(user)
    client = _client(
        users=FakeUsersRepository(user),
        sessions=FakeSessionsRepository(session),
        admin_emails=[user.email],
    )
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/is-admin")

    assert response.text == "True"


def test_admin_email_match_is_case_insensitive_on_the_gate() -> None:
    user = _user(UserStatus.ACTIVE)
    session = _session(user)
    client = _client(
        users=FakeUsersRepository(user),
        sessions=FakeSessionsRepository(session),
        admin_emails=[user.email.upper()],
    )
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/is-admin")

    assert response.text == "True"


def test_pending_user_status_screen_is_an_htmx_partial_for_an_hx_request() -> None:
    user = _user(UserStatus.PENDING)
    session = _session(user)
    client = _client(users=FakeUsersRepository(user), sessions=FakeSessionsRepository(session))
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/protected", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text
