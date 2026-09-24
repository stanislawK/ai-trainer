"""End-to-end proof of ticket #44's ACs against a real Postgres-backed session — the
router-level counterpart to `tests/unit/test_errors.py`, `test_active_user_gate.py` and
`test_web_csrf.py`, which all use fakes. Builds the same middleware stack as
`tests/integration/test_active_user_gate.py`, plus `CsrfMiddleware` and the new error handlers,
since AC1, AC3 and AC5 each depend on more than one of these working together."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CsrfMiddleware
from ai_trainer.web.errors import register_error_handlers
from ai_trainer.web.templating import build_templates

FROZEN_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


def _client(
    *,
    users: SqlAlchemyUsersRepository,
    sessions: SqlAlchemySessionsRepository,
) -> TestClient:
    app = FastAPI()
    templates = build_templates()
    register_error_handlers(app, templates)
    # Same ordering as `main.py`: `CsrfMiddleware` is added last so it ends up outermost,
    # running before the gate on every request.
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
        templates=templates,
        admin_emails=[],
    )
    app.add_middleware(CsrfMiddleware, secret_key=b"integration-test-secret", templates=templates)

    @app.post("/action")
    async def action() -> PlainTextResponse:
        return PlainTextResponse("done")

    # `raise_server_exceptions=False`: see `tests/unit/test_errors.py` for why.
    return TestClient(app, raise_server_exceptions=False)


async def test_unknown_url_for_a_signed_in_active_user_returns_the_designed_404_page(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(
            sub="sub-404", email="a@example.com", name="A", locale="en", status=UserStatus.ACTIVE
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    client = _client(users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/nowhere-real")

    assert response.status_code == 404
    assert "Off route" in response.text
    assert 'href="/"' in response.text


async def test_unauthenticated_request_to_an_unknown_url_is_still_401_not_404(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """The gate runs before routing (ADR-0005 invariant 2): an unauthenticated visitor never
    learns whether a route exists, even with the new 404 handler wired in."""
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    client = _client(users=users, sessions=sessions)

    response = client.get("/nowhere-real")

    assert response.status_code == 401
    assert 'href="/sign-in"' in response.text


async def test_pending_user_sees_the_status_page_on_an_unknown_url_too(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """AC5: a pending/disabled user sees only the status page, whatever URL they open — even
    one that matches no route at all, not just `/`."""
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(
            sub="sub-pending",
            email="b@example.com",
            name="B",
            locale="en",
            status=UserStatus.PENDING,
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    client = _client(users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.get("/some/unknown/path")

    assert response.status_code == 200
    assert "on the list" in response.text


async def test_bad_csrf_token_returns_the_designed_403_page(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(
            sub="sub-csrf", email="c@example.com", name="C", locale="en", status=UserStatus.ACTIVE
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    client = _client(users=users, sessions=sessions)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))

    response = client.post("/action")

    assert response.status_code == 403
    assert "data-reload-page" in response.text
