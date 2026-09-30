from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ai_trainer.domain.goals import Goal
from ai_trainer.domain.sessions import NewSession, Session
from ai_trainer.domain.users import NewUser, User, UserAlreadyExistsError, UserStatus
from scripts.dev_session import (
    DEV_FRESH_EMAIL,
    DEV_FRESH_SUB,
    DEV_SESSION_EMAIL,
    DEV_SESSION_SUB,
    build_cookie_payload,
    parse_args,
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

    async def list_all(self) -> list[User]:
        raise NotImplementedError

    async def delete(self, user_id: UUID) -> None:
        raise NotImplementedError


class FakeOnboardingRepository:
    def __init__(self) -> None:
        self.onboarded_at: dict[UUID, datetime | None] = {}
        self.sports: dict[UUID, list[str]] = {}
        self.goals: dict[UUID, list[Goal]] = {}

    async def replace_sports(
        self,
        user_id: UUID,
        sport_ids: Sequence[str],
        *,
        general_goals_of: Sequence[str] = (),
        delete_goals_of: Sequence[str] = (),
    ) -> None:
        self.sports[user_id] = list(sport_ids)

    async def list_sports(self, user_id: UUID) -> Sequence[str]:
        return self.sports.get(user_id, [])

    async def replace_availability(
        self, user_id: UUID, minutes_by_weekday: Mapping[int, int]
    ) -> None:
        raise NotImplementedError

    async def list_availability(self, user_id: UUID) -> Mapping[int, int]:
        raise NotImplementedError

    async def replace_goals(self, user_id: UUID, goals: Sequence[Goal]) -> None:
        self.goals[user_id] = list(goals)

    async def list_goals(self, user_id: UUID) -> Sequence[Goal]:
        return self.goals.get(user_id, [])

    async def set_onboarded_at(self, user_id: UUID, when: datetime | None) -> None:
        self.onboarded_at[user_id] = when


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

    session = await seed_dev_session(
        users, sessions, FakeClock(), onboarding=FakeOnboardingRepository(), session_ttl=SESSION_TTL
    )

    seeded_user = await users.get_by_sub(DEV_SESSION_SUB)
    assert seeded_user is not None
    assert seeded_user.status is UserStatus.ACTIVE
    assert seeded_user.email == DEV_SESSION_EMAIL
    assert session.user_id == seeded_user.id
    assert session.expires_at == FROZEN_NOW + SESSION_TTL


async def test_second_run_reuses_the_seeded_user_but_opens_a_new_session() -> None:
    users = FakeUsersRepository()
    sessions = FakeSessionsRepository()

    first = await seed_dev_session(
        users, sessions, FakeClock(), onboarding=FakeOnboardingRepository(), session_ttl=SESSION_TTL
    )
    second = await seed_dev_session(
        users, sessions, FakeClock(), onboarding=FakeOnboardingRepository(), session_ttl=SESSION_TTL
    )

    assert users.create_calls == 1
    assert second.user_id == first.user_id
    assert second.id != first.id


async def test_concurrent_run_recovers_by_fetching_the_race_winner() -> None:
    users = RacingUsersRepository()
    sessions = FakeSessionsRepository()

    session = await seed_dev_session(
        users, sessions, FakeClock(), onboarding=FakeOnboardingRepository(), session_ttl=SESSION_TTL
    )

    assert users.create_calls == 1
    seeded_user = await users.get_by_sub(DEV_SESSION_SUB)
    assert seeded_user is not None
    assert session.user_id == seeded_user.id


async def test_concurrent_run_raises_if_the_race_winner_is_not_found() -> None:
    """Defensive: never expected to happen in practice (mirrors `sign_in_with_google`)."""
    users = VanishingUsersRepository()
    sessions = FakeSessionsRepository()

    with pytest.raises(RuntimeError, match="vanished"):
        await seed_dev_session(
            users,
            sessions,
            FakeClock(),
            onboarding=FakeOnboardingRepository(),
            session_ttl=SESSION_TTL,
        )


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


async def test_default_seed_marks_the_user_onboarded_now() -> None:
    users = FakeUsersRepository()
    onboarding = FakeOnboardingRepository()

    session = await seed_dev_session(
        users, FakeSessionsRepository(), FakeClock(), onboarding=onboarding, session_ttl=SESSION_TTL
    )

    assert onboarding.onboarded_at == {session.user_id: FROZEN_NOW}


async def test_default_seed_marks_an_already_seeded_but_not_onboarded_user_onboarded() -> None:
    """A dev database created before onboarding existed has the seeded user with a null
    `onboarded_at`; the e2e specs must not suddenly land on the sports step."""
    users = FakeUsersRepository()
    onboarding = FakeOnboardingRepository()
    sessions = FakeSessionsRepository()
    first = await seed_dev_session(
        users, sessions, FakeClock(), onboarding=onboarding, session_ttl=SESSION_TTL
    )
    onboarding.onboarded_at.clear()

    await seed_dev_session(
        users, sessions, FakeClock(), onboarding=onboarding, session_ttl=SESSION_TTL
    )

    assert onboarding.onboarded_at == {first.user_id: FROZEN_NOW}


async def test_not_onboarded_seed_uses_a_separate_user_left_without_onboarded_at() -> None:
    users = FakeUsersRepository()
    onboarding = FakeOnboardingRepository()

    session = await seed_dev_session(
        users,
        FakeSessionsRepository(),
        FakeClock(),
        onboarding=onboarding,
        session_ttl=SESSION_TTL,
        onboarded=False,
    )

    fresh = await users.get_by_sub(DEV_FRESH_SUB)
    assert fresh is not None
    assert fresh.email == DEV_FRESH_EMAIL
    assert fresh.status is UserStatus.ACTIVE
    assert session.user_id == fresh.id
    assert await users.get_by_sub(DEV_SESSION_SUB) is None
    assert onboarding.onboarded_at == {fresh.id: None}


async def test_not_onboarded_seed_resets_the_sports_of_an_earlier_run() -> None:
    users = FakeUsersRepository()
    onboarding = FakeOnboardingRepository()
    sessions = FakeSessionsRepository()
    first = await seed_dev_session(
        users,
        sessions,
        FakeClock(),
        onboarding=onboarding,
        session_ttl=SESSION_TTL,
        onboarded=False,
    )
    onboarding.sports[first.user_id] = ["climbing"]
    onboarding.onboarded_at[first.user_id] = FROZEN_NOW

    await seed_dev_session(
        users,
        sessions,
        FakeClock(),
        onboarding=onboarding,
        session_ttl=SESSION_TTL,
        onboarded=False,
    )

    assert onboarding.sports[first.user_id] == []
    assert onboarding.onboarded_at[first.user_id] is None


async def test_not_onboarded_seed_resets_the_goals_of_an_earlier_run() -> None:
    users = FakeUsersRepository()
    onboarding = FakeOnboardingRepository()
    sessions = FakeSessionsRepository()
    first = await seed_dev_session(
        users,
        sessions,
        FakeClock(),
        onboarding=onboarding,
        session_ttl=SESSION_TTL,
        onboarded=False,
    )
    onboarding.goals[first.user_id] = [Goal(text="Climb 7a", target_date=None)]

    await seed_dev_session(
        users,
        sessions,
        FakeClock(),
        onboarding=onboarding,
        session_ttl=SESSION_TTL,
        onboarded=False,
    )

    assert onboarding.goals[first.user_id] == []


def test_parse_args_defaults_to_an_onboarded_user() -> None:
    assert parse_args([]).not_onboarded is False


def test_parse_args_not_onboarded_flag() -> None:
    assert parse_args(["--not-onboarded"]).not_onboarded is True
