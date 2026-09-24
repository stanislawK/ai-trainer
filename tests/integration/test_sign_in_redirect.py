"""`GET /sign-in` redirects an already-signed-in active user to `/`, and otherwise renders the
sign-in page (ADR-0005, ADR-0019, ticket #43)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.web.sign_in import build_sign_in_router
from ai_trainer.web.templating import STATIC_DIR, build_templates

FROZEN_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


def _client(
    session_factory: Callable[[], AsyncSession],
    *,
    users: SqlAlchemyUsersRepository,
    sessions: SqlAlchemySessionsRepository,
) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_sign_in_router(
            templates=build_templates(), users=users, sessions=sessions, clock=FakeClock()
        )
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return TestClient(app)


async def test_signed_out_visit_to_sign_in_shows_the_page(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    client = _client(db_session_factory, users=users, sessions=sessions)

    response = client.get("/sign-in", follow_redirects=False)

    assert response.status_code == 200
    assert "Continue with Google" in response.text


async def test_signed_in_active_user_visiting_sign_in_is_redirected_to_home(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(
            sub="sub-active",
            email="active@example.com",
            name="Active",
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set("session_id", str(session.id))

    response = client.get("/sign-in", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


async def test_signed_in_pending_user_visiting_sign_in_still_sees_the_sign_in_page(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """`/sign-in` is exempt from `ActiveUserGateMiddleware` (AC), so a `pending` user's own
    session never reaches the status screen here; the route only redirects an `ACTIVE` user
    away, and otherwise falls through to the ordinary sign-in render."""
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(
            sub="sub-pending",
            email="pending@example.com",
            name="Pending",
            locale="en",
            status=UserStatus.PENDING,
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + timedelta(days=1))
    )
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set("session_id", str(session.id))

    response = client.get("/sign-in", follow_redirects=False)

    assert response.status_code == 200
    assert "Continue with Google" in response.text


async def test_expired_session_cookie_still_shows_the_sign_in_page(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    users = SqlAlchemyUsersRepository(db_session_factory)
    sessions = SqlAlchemySessionsRepository(db_session_factory)
    user = await users.create(
        NewUser(
            sub="sub-expired",
            email="expired@example.com",
            name="Expired",
            locale="en",
            status=UserStatus.ACTIVE,
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW - timedelta(days=1))
    )
    client = _client(db_session_factory, users=users, sessions=sessions)
    client.cookies.set("session_id", str(session.id))

    response = client.get("/sign-in", follow_redirects=False)

    assert response.status_code == 200
    assert "Continue with Google" in response.text
