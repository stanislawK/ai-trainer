from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import NewUser, User, UserAlreadyExistsError, UserStatus
from scripts.dev_session import (
    DEV_SESSION_EMAIL,
    DEV_SESSION_SUB,
    build_cookie_payload,
    seed_dev_session,
)

FROZEN_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
SESSION_TTL = timedelta(days=14)


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class FakeUsersRepository:
    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.create_calls = 0

    async def get_by_sub(self, sub: str) -> User | None:
        return self.users.get(sub)

    async def get(self, user_id: UUID) -> User | None:
        for user in self.users.values():
            if user.id == user_id:
                return user
        return None

    async def create(self, new_user: NewUser) -> User:
        self.create_calls += 1
        user = User(id=uuid4(), created_at=FROZEN_NOW, **new_user.model_dump())
        self.users[new_user.sub] = user
        return user


class FakeSessionsRepository:
    def __init__(self) -> None:
        self.sessions: dict[UUID, Session] = {}

    async def create(self, new_session: NewSession) -> Session:
        session = Session(id=uuid4(), created_at=FROZEN_NOW, **new_session.model_dump())
        self.sessions[session.id] = session
        return session

    async def get(self, session_id: UUID) -> Session | None:
        return self.sessions.get(session_id)

    async def delete(self, session_id: UUID) -> None:
        self.sessions.pop(session_id, None)


class RacingUsersRepository(FakeUsersRepository):
    """Another `dev_session.py` invocation wins the race to create the same seeded user
    first: by the time this `create` call fails, the winner's row is already visible."""

    async def create(self, new_user: NewUser) -> User:
        self.create_calls += 1
        winner = User(id=uuid4(), created_at=FROZEN_NOW, **new_user.model_dump())
        self.users[new_user.sub] = winner
        raise UserAlreadyExistsError(new_user.sub)


class VanishingUsersRepository(FakeUsersRepository):
    async def create(self, new_user: NewUser) -> User:
        raise UserAlreadyExistsError(new_user.sub)


async def test_first_run_creates_an_active_seeded_user_and_a_session() -> None:
    users = FakeUsersRepository()
    sessions = FakeSessionsRepository()

    session = await seed_dev_session(users, sessions, FakeClock(), session_ttl=SESSION_TTL)

    seeded_user = await users.get_by_sub(DEV_SESSION_SUB)
    assert seeded_user is not None
    assert seeded_user.status is UserStatus.ACTIVE
    assert seeded_user.email == DEV_SESSION_EMAIL
    assert session.user_id == seeded_user.id
    assert session.expires_at == FROZEN_NOW + SESSION_TTL


async def test_second_run_reuses_the_seeded_user_but_opens_a_new_session() -> None:
    users = FakeUsersRepository()
    sessions = FakeSessionsRepository()

    first = await seed_dev_session(users, sessions, FakeClock(), session_ttl=SESSION_TTL)
    second = await seed_dev_session(users, sessions, FakeClock(), session_ttl=SESSION_TTL)

    assert users.create_calls == 1
    assert second.user_id == first.user_id
    assert second.id != first.id


async def test_concurrent_run_recovers_by_fetching_the_race_winner() -> None:
    users = RacingUsersRepository()
    sessions = FakeSessionsRepository()

    session = await seed_dev_session(users, sessions, FakeClock(), session_ttl=SESSION_TTL)

    assert users.create_calls == 1
    seeded_user = await users.get_by_sub(DEV_SESSION_SUB)
    assert seeded_user is not None
    assert session.user_id == seeded_user.id


async def test_concurrent_run_raises_if_the_race_winner_is_not_found() -> None:
    """Defensive: never expected to happen in practice (mirrors `sign_in_with_google`)."""
    users = VanishingUsersRepository()
    sessions = FakeSessionsRepository()

    with pytest.raises(RuntimeError, match="vanished"):
        await seed_dev_session(users, sessions, FakeClock(), session_ttl=SESSION_TTL)


def test_build_cookie_payload_matches_the_real_sign_in_cookie_shape() -> None:
    session = Session(
        id=uuid4(),
        user_id=uuid4(),
        created_at=FROZEN_NOW,
        expires_at=FROZEN_NOW + SESSION_TTL,
    )

    payload = build_cookie_payload(session, secure=True)

    assert payload == {
        "name": "session_id",
        "value": str(session.id),
        "domain": "localhost",
        "path": "/",
        "expires": session.expires_at.timestamp(),
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


def test_build_cookie_payload_carries_through_secure_false() -> None:
    session = Session(
        id=uuid4(), user_id=uuid4(), created_at=FROZEN_NOW, expires_at=FROZEN_NOW + SESSION_TTL
    )

    payload = build_cookie_payload(session, secure=False)

    assert payload["secure"] is False
