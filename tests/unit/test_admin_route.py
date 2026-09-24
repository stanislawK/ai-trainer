"""`GET /admin`: a stub landing page gated on admin rights derived per request (ADR-0005),
scaffolding the nav item and route for #16 to fill in with the real user table. Uses the same
`ActiveUserGateMiddleware` fakes as `test_active_user_gate.py`."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.admin import build_admin_router
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.templating import build_templates

FROZEN_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


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


def _user(email: str = "athlete@example.com") -> User:
    return User(
        id=uuid4(),
        sub="google-sub-1",
        email=email,
        name="Athlete",
        locale="en",
        status=UserStatus.ACTIVE,
        created_at=FROZEN_NOW,
    )


def _session(user: User) -> Session:
    return Session(
        id=uuid4(),
        user_id=user.id,
        expires_at=FROZEN_NOW + timedelta(days=1),
        created_at=FROZEN_NOW,
    )


def _client(*, admin_emails: list[str]) -> tuple[TestClient, User, Session]:
    user = _user()
    session = _session(user)
    app = FastAPI()
    templates = build_templates()
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=FakeUsersRepository(user),
        sessions=FakeSessionsRepository(session),
        clock=FakeClock(),
        templates=templates,
        admin_emails=admin_emails,
    )
    app.include_router(build_admin_router(templates))
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    return client, user, session


def test_non_admin_gets_403_from_the_admin_page() -> None:
    client, _, _ = _client(admin_emails=[])

    response = client.get("/admin")

    assert response.status_code == 403
    assert "admin" in response.text.lower()


def test_admin_reaches_the_admin_page() -> None:
    client, _, _ = _client(admin_emails=["athlete@example.com"])

    response = client.get("/admin")

    assert response.status_code == 200
    assert "Admin" in response.text


def test_non_admin_forbidden_response_is_an_htmx_partial_for_an_hx_request() -> None:
    client, _, _ = _client(admin_emails=[])

    response = client.get("/admin", headers={"HX-Request": "true"})

    assert response.status_code == 403
    assert "<html" not in response.text
