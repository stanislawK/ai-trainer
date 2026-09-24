"""Seeds a dev-only active user and session, and prints the cookie as JSON (ADR-0013).

Run with `uv run python scripts/dev_session.py` (or `make e2e`, which runs it via
`tests/e2e/conftest.py`). It reaches the same PostgreSQL the app uses (`DATABASE_URL`), so
`docker compose up -d db` (or the full stack) must already be running. It is a script, never
a route: no authentication bypass ships in the app.

The printed JSON is shaped for Playwright's `BrowserContext.add_cookies`, matching the
cookie `ai_trainer.web.auth.build_auth_router`'s `/auth/callback` sets on a real sign-in.
"""

import asyncio
import json
import sys
from datetime import timedelta
from typing import Any, TextIO

from ai_trainer.adapters.clock import UtcClock
from ai_trainer.adapters.db import build_engine, build_session_factory
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.sessions import SessionsRepositoryPort
from ai_trainer.application.ports.users import UsersRepositoryPort
from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import NewUser, UserAlreadyExistsError, UserStatus
from ai_trainer.settings import Settings
from ai_trainer.web.auth import SESSION_COOKIE_NAME

DEV_SESSION_SUB = "dev-session-script"
DEV_SESSION_EMAIL = "dev-session@example.com"


async def seed_dev_session(
    users: UsersRepositoryPort,
    sessions: SessionsRepositoryPort,
    clock: ClockPort,
    *,
    session_ttl: timedelta,
) -> Session:
    """Gets or creates the seeded dev-session user (always `active`) and opens a fresh
    session for it, mirroring `application.auth.sign_in_with_google`'s get-or-create shape."""
    user = await users.get_by_sub(DEV_SESSION_SUB)
    if user is None:
        try:
            user = await users.create(
                NewUser(
                    sub=DEV_SESSION_SUB,
                    email=DEV_SESSION_EMAIL,
                    name="Dev session",
                    locale="en",
                    status=UserStatus.ACTIVE,
                )
            )
        except UserAlreadyExistsError:
            # Another concurrent `dev_session.py` run won the race; its row is now visible.
            user = await users.get_by_sub(DEV_SESSION_SUB)
            if user is None:
                raise RuntimeError(
                    f"user {DEV_SESSION_SUB!r} vanished after a concurrent create race"
                ) from None
    return await sessions.create(NewSession(user_id=user.id, expires_at=clock.now() + session_ttl))


def build_cookie_payload(session: Session, *, secure: bool) -> dict[str, Any]:
    """Cookie dict in Playwright's `BrowserContext.add_cookies` shape. `domain`/`path` are
    hardcoded: this is a dev-only tool that only ever targets the local compose stack."""
    return {
        "name": SESSION_COOKIE_NAME,
        "value": str(session.id),
        "domain": "localhost",
        "path": "/",
        "expires": session.expires_at.timestamp(),
        "httpOnly": True,
        "secure": secure,
        "sameSite": "Lax",
    }


async def main(*, settings: Settings | None = None, output: TextIO = sys.stdout) -> None:
    settings = settings if settings is not None else Settings()
    engine = build_engine(str(settings.database_url))
    try:
        session_factory = build_session_factory(engine)
        users = SqlAlchemyUsersRepository(session_factory)
        sessions = SqlAlchemySessionsRepository(session_factory)
        session = await seed_dev_session(
            users,
            sessions,
            UtcClock(),
            session_ttl=timedelta(days=settings.session_ttl_days),
        )
    finally:
        await engine.dispose()
    cookie = build_cookie_payload(session, secure=settings.session_cookie_secure)
    output.write(json.dumps(cookie) + "\n")


if __name__ == "__main__":
    asyncio.run(main())  # pragma: no cover -- only runs via direct script execution, not import
