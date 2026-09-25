from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.auth import resolve_authenticated_user, sign_in_with_google, sign_out
from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import GoogleClaims, NewUser, User, UserAlreadyExistsError, UserStatus

FROZEN_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
SESSION_TTL = timedelta(days=14)
ADMIN_EMAILS = ["admin@example.com"]


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

    async def list_all(self) -> list[User]:
        raise NotImplementedError

    async def delete(self, user_id: UUID) -> None:
        sub = next((s for s, user in self.users.items() if user.id == user_id), None)
        if sub is not None:
            del self.users[sub]


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

    async def delete_for_user(self, user_id: UUID) -> None:
        raise NotImplementedError


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


async def test_first_sign_in_creates_a_pending_user_and_a_session() -> None:
    users = FakeUsersRepository()
    sessions = FakeSessionsRepository()

    user, session = await sign_in_with_google(
        _claims(),
        admin_emails=ADMIN_EMAILS,
        session_ttl=SESSION_TTL,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
    )

    assert user.status is UserStatus.PENDING
    assert session.user_id == user.id
    assert session.expires_at == FROZEN_NOW + SESSION_TTL


async def test_verified_admin_email_is_created_active() -> None:
    users = FakeUsersRepository()
    sessions = FakeSessionsRepository()

    user, _ = await sign_in_with_google(
        _claims(email="admin@example.com"),
        admin_emails=ADMIN_EMAILS,
        session_ttl=SESSION_TTL,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
    )

    assert user.status is UserStatus.ACTIVE


async def test_second_sign_in_with_the_same_sub_reuses_the_user() -> None:
    users = FakeUsersRepository()
    sessions = FakeSessionsRepository()

    first_user, _ = await sign_in_with_google(
        _claims(),
        admin_emails=ADMIN_EMAILS,
        session_ttl=SESSION_TTL,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
    )
    second_user, second_session = await sign_in_with_google(
        _claims(),
        admin_emails=ADMIN_EMAILS,
        session_ttl=SESSION_TTL,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
    )

    assert second_user.id == first_user.id
    assert users.create_calls == 1
    assert second_session.user_id == first_user.id


class RacingUsersRepository(FakeUsersRepository):
    """Simulates another request's sign-in winning the race to create the same `sub` first:
    by the time this `create` call fails, the winner's row is already visible to `get_by_sub`."""

    async def create(self, new_user: NewUser) -> User:
        self.create_calls += 1
        winner = User(id=uuid4(), created_at=FROZEN_NOW, **new_user.model_dump())
        self.users[new_user.sub] = winner
        raise UserAlreadyExistsError(new_user.sub)


async def test_concurrent_sign_in_recovers_by_fetching_the_race_winner() -> None:
    users = RacingUsersRepository()
    sessions = FakeSessionsRepository()

    user, session = await sign_in_with_google(
        _claims(),
        admin_emails=ADMIN_EMAILS,
        session_ttl=SESSION_TTL,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
    )

    assert users.create_calls == 1
    assert session.user_id == user.id


class VanishingUsersRepository(FakeUsersRepository):
    """A create call reports a race loss, but the winner's row is (impossibly) not there
    on retry — exercises the defensive guard, never expected to happen in practice."""

    async def create(self, new_user: NewUser) -> User:
        raise UserAlreadyExistsError(new_user.sub)


async def test_concurrent_sign_in_raises_if_the_race_winner_is_not_found() -> None:
    users = VanishingUsersRepository()
    sessions = FakeSessionsRepository()

    with pytest.raises(RuntimeError, match="vanished"):
        await sign_in_with_google(
            _claims(),
            admin_emails=ADMIN_EMAILS,
            session_ttl=SESSION_TTL,
            users=users,
            sessions=sessions,
            clock=FakeClock(),
        )


async def test_sign_out_deletes_the_session() -> None:
    sessions = FakeSessionsRepository()
    session = await sessions.create(NewSession(user_id=uuid4(), expires_at=FROZEN_NOW))

    await sign_out(session.id, sessions)

    assert await sessions.get(session.id) is None


async def test_resolve_authenticated_user_returns_the_session_owner() -> None:
    users = FakeUsersRepository()
    sessions = FakeSessionsRepository()
    user, _ = await sign_in_with_google(
        _claims(),
        admin_emails=ADMIN_EMAILS,
        session_ttl=SESSION_TTL,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=FROZEN_NOW + SESSION_TTL)
    )

    resolved = await resolve_authenticated_user(
        session.id, sessions=sessions, users=users, clock=FakeClock()
    )

    assert resolved is not None
    assert resolved.id == user.id


async def test_resolve_authenticated_user_returns_none_for_an_unknown_session() -> None:
    resolved = await resolve_authenticated_user(
        uuid4(), sessions=FakeSessionsRepository(), users=FakeUsersRepository(), clock=FakeClock()
    )

    assert resolved is None


async def test_resolve_authenticated_user_returns_none_for_an_expired_session() -> None:
    users = FakeUsersRepository()
    sessions = FakeSessionsRepository()
    session = await sessions.create(
        NewSession(user_id=uuid4(), expires_at=FROZEN_NOW - timedelta(seconds=1))
    )

    resolved = await resolve_authenticated_user(
        session.id, sessions=sessions, users=users, clock=FakeClock()
    )

    assert resolved is None


async def test_resolve_authenticated_user_returns_none_when_the_user_no_longer_exists() -> None:
    """Defensive: the session row outlives its user only if `ON DELETE CASCADE` (ADR-0004)
    somehow didn't fire; resolving must not crash on that."""
    sessions = FakeSessionsRepository()
    session = await sessions.create(
        NewSession(user_id=uuid4(), expires_at=FROZEN_NOW + SESSION_TTL)
    )

    resolved = await resolve_authenticated_user(
        session.id, sessions=sessions, users=FakeUsersRepository(), clock=FakeClock()
    )

    assert resolved is None


async def test_signing_in_again_after_deletion_creates_a_fresh_pending_account() -> None:
    """G7: a deleted user signing in again with the same Google account is a brand new
    account, not a resurrection of the old one — same `sub`, a new `id`, `pending` again."""
    users = FakeUsersRepository()
    sessions = FakeSessionsRepository()
    first_user, _ = await sign_in_with_google(
        _claims(),
        admin_emails=ADMIN_EMAILS,
        session_ttl=SESSION_TTL,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
    )
    await users.delete(first_user.id)

    second_user, _ = await sign_in_with_google(
        _claims(),
        admin_emails=ADMIN_EMAILS,
        session_ttl=SESSION_TTL,
        users=users,
        sessions=sessions,
        clock=FakeClock(),
    )

    assert second_user.id != first_user.id
    assert second_user.sub == first_user.sub
    assert second_user.status is UserStatus.PENDING
