"""Authenticating an admin token on the knowledge API (ADR-0036 invariant 2, #457), on in-memory
repositories: the hash, expiry and revocation, then `active` and `ADMIN_EMAILS`, re-checked on
every request; `last_used_at` set only when all of it holds."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.admin_tokens import (
    AdminTokens,
    TokenAuthenticated,
    TokenAuthenticator,
    TokenNotAdmin,
    TokenRejected,
)
from ai_trainer.domain.users import NewUser, User, UserStatus
from tests.admin_tokens_support import MemoryAdminTokens

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
ADMIN = "admin@example.com"


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> datetime:
        return self.at


class MemoryUsers:
    """The `get` the authenticator reads; nothing else is called."""

    def __init__(self) -> None:
        self.rows: dict[UUID, User] = {}

    def add(self, email: str = ADMIN, status: UserStatus = UserStatus.ACTIVE) -> User:
        user = User(
            id=uuid4(),
            sub=f"sub-{uuid4()}",
            email=email,
            name="Admin",
            locale="en",
            status=status,
            created_at=NOW,
        )
        self.rows[user.id] = user
        return user

    async def get(self, user_id: UUID) -> User | None:
        return self.rows.get(user_id)

    async def get_by_sub(self, sub: str) -> User | None:
        raise AssertionError("not read")

    async def create(self, new_user: NewUser) -> User:
        raise AssertionError("not called")

    async def list_all(self) -> Sequence[User]:
        raise AssertionError("not called")

    async def delete(self, user_id: UUID) -> None:
        raise AssertionError("not called")


class World:
    def __init__(self, admin_emails: Sequence[str] = (ADMIN,)) -> None:
        self.clock = Clock()
        self.tokens = MemoryAdminTokens()
        self.users = MemoryUsers()
        self.issuer = AdminTokens(tokens=self.tokens, clock=self.clock)
        self.authenticator = TokenAuthenticator(
            tokens=self.tokens, users=self.users, clock=self.clock, admin_emails=admin_emails
        )

    async def issue(self, user: User, days: int = 30) -> str:
        return (await self.issuer.issue(user.id, days=days)).secret


async def test_a_valid_token_of_an_active_admin_authenticates_and_is_touched() -> None:
    world = World()
    user = world.users.add()
    secret = await world.issue(user)
    world.clock.at = NOW + timedelta(hours=1)

    outcome = await world.authenticator.authenticate(secret)

    [token_id] = world.tokens.rows
    assert outcome == TokenAuthenticated(user_id=user.id, token_id=token_id)
    assert world.tokens.used[token_id] == NOW + timedelta(hours=1)


async def test_the_admin_email_matches_case_insensitively() -> None:
    world = World(admin_emails=["Admin@Example.COM"])
    user = world.users.add()

    outcome = await world.authenticator.authenticate(await world.issue(user))

    assert isinstance(outcome, TokenAuthenticated)


async def test_an_unknown_token_is_rejected() -> None:
    world = World()
    world.users.add()
    unknown = "atk_" + "A" * 43

    assert await world.authenticator.authenticate(unknown) == TokenRejected()


async def test_an_expired_token_is_rejected_and_not_touched() -> None:
    world = World()
    secret = await world.issue(world.users.add(), days=1)
    world.clock.at = NOW + timedelta(days=1)

    assert await world.authenticator.authenticate(secret) == TokenRejected()
    assert world.tokens.used == {}


async def test_a_revoked_token_is_rejected_and_not_touched() -> None:
    world = World()
    user = world.users.add()
    secret = await world.issue(user)
    [token_id] = world.tokens.rows
    await world.issuer.revoke(user.id, token_id)

    assert await world.authenticator.authenticate(secret) == TokenRejected()
    assert world.tokens.used == {}


@pytest.mark.parametrize(
    "secret",
    ["", "atk_", "atk_" + "A" * 42, "atk_" + "A" * 44, "xtk_" + "A" * 43, "atk_" + "A" * 42 + "="],
    ids=["empty", "prefix-only", "short", "long", "wrong-prefix", "bad-character"],
)
async def test_a_malformed_token_is_rejected_without_a_lookup(secret: str) -> None:
    world = World()

    assert await world.authenticator.authenticate(secret) == TokenRejected()
    assert world.tokens.lookups == []


@pytest.mark.parametrize("status", [UserStatus.PENDING, UserStatus.DISABLED])
async def test_a_token_whose_user_is_not_active_is_not_admin(status: UserStatus) -> None:
    world = World()
    user = world.users.add()
    secret = await world.issue(user)
    world.users.rows[user.id] = user.model_copy(update={"status": status})

    assert await world.authenticator.authenticate(secret) == TokenNotAdmin()
    assert world.tokens.used == {}


async def test_a_token_whose_user_left_admin_emails_is_not_admin() -> None:
    world = World()
    user = world.users.add(email="former@example.com")
    secret = await world.issue(user)

    assert await world.authenticator.authenticate(secret) == TokenNotAdmin()
    assert world.tokens.used == {}


async def test_a_token_whose_user_is_gone_is_rejected() -> None:
    # The table cascades on the user's delete; this covers a row read just before it.
    world = World()
    user = world.users.add()
    secret = await world.issue(user)
    del world.users.rows[user.id]

    assert await world.authenticator.authenticate(secret) == TokenRejected()


async def test_the_outcomes_repr_carries_no_secret() -> None:
    world = World()
    user = world.users.add()
    secret = await world.issue(user)

    outcome = await world.authenticator.authenticate(secret)

    assert secret not in repr(outcome)
    assert secret[4:] not in repr(world.tokens.rows)
