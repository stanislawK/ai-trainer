"""`SqlAlchemyAdminTokensRepository` on real PostgreSQL (ADR-0036, #455): user-scoped add, list
and revoke."""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.admin_token_repository import SqlAlchemyAdminTokensRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.admin_tokens import NewAdminToken, TokenState
from ai_trainer.domain.users import NewUser, User, UserStatus

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


async def _admin(factory: Callable[[], AsyncSession], sub: str) -> User:
    return await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=sub, locale="en", status=UserStatus.ACTIVE
        )
    )


def _new(user: User, *, at: datetime = NOW, days: int = 7) -> NewAdminToken:
    marker = uuid4().hex
    return NewAdminToken(
        id=uuid4(),
        user_id=user.id,
        token_hash=hashlib.sha256(marker.encode()).hexdigest(),
        display_prefix=f"atk_{marker[:8]}",
        created_at=at,
        expires_at=at + timedelta(days=days),
    )


async def test_add_stores_the_hash_and_returns_the_token_without_it(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyAdminTokensRepository(db_session_factory)
    admin = await _admin(db_session_factory, "tokens-add")
    new = _new(admin)

    stored = await repository.add(new)

    assert stored.id == new.id
    assert stored.display_prefix == new.display_prefix
    assert stored.expires_at == new.expires_at
    assert stored.revoked_at is None
    assert stored.last_used_at is None
    async with db_session_factory() as session:
        stored_hash = await session.scalar(
            text("SELECT token_hash FROM admin_tokens WHERE id = :id"), {"id": new.id}
        )
    assert stored_hash == new.token_hash


async def test_list_for_user_is_newest_first_and_only_that_users(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyAdminTokensRepository(db_session_factory)
    me = await _admin(db_session_factory, "tokens-list-me")
    other = await _admin(db_session_factory, "tokens-list-other")
    older = await repository.add(_new(me))
    newer = await repository.add(_new(me, at=NOW + timedelta(hours=1)))
    await repository.add(_new(other))

    listed = await repository.list_for_user(me.id)

    assert [token.id for token in listed] == [newer.id, older.id]


async def test_revoke_sets_revoked_at_on_an_active_token(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyAdminTokensRepository(db_session_factory)
    me = await _admin(db_session_factory, "tokens-revoke")
    token = await repository.add(_new(me))
    at = NOW + timedelta(minutes=10)

    revoked = await repository.revoke(me.id, token.id, at=at)

    assert revoked is not None
    assert revoked.revoked_at == at
    [listed] = await repository.list_for_user(me.id)
    assert listed.state_at(at) is TokenState.REVOKED


async def test_revoke_leaves_a_revoked_or_expired_token_as_it_is(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyAdminTokensRepository(db_session_factory)
    me = await _admin(db_session_factory, "tokens-revoke-twice")
    revoked_once = await repository.add(_new(me))
    expired = await repository.add(_new(me, days=1))
    first = NOW + timedelta(minutes=1)
    await repository.revoke(me.id, revoked_once.id, at=first)

    again = await repository.revoke(me.id, revoked_once.id, at=NOW + timedelta(hours=2))
    after_expiry = await repository.revoke(me.id, expired.id, at=NOW + timedelta(days=2))

    assert again is not None
    assert again.revoked_at == first
    assert after_expiry is not None
    assert after_expiry.revoked_at is None


async def test_one_admin_cannot_revoke_anothers_token(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyAdminTokensRepository(db_session_factory)
    owner = await _admin(db_session_factory, "tokens-tenancy-owner")
    intruder = await _admin(db_session_factory, "tokens-tenancy-intruder")
    token = await repository.add(_new(owner))

    assert await repository.revoke(intruder.id, token.id, at=NOW) is None
    assert await repository.revoke(owner.id, uuid4(), at=NOW) is None
    assert await repository.list_for_user(intruder.id) == []
    [still_active] = await repository.list_for_user(owner.id)
    assert still_active.revoked_at is None


async def test_get_by_hash_finds_the_token_and_nothing_for_another_hash(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """The authentication lookup (#457): by hash alone, since the token is the identity."""
    repository = SqlAlchemyAdminTokensRepository(db_session_factory)
    admin = await _admin(db_session_factory, "tokens-by-hash")
    new = _new(admin)
    await repository.add(new)

    found = await repository.get_by_hash(new.token_hash)
    missing = await repository.get_by_hash(hashlib.sha256(b"never issued").hexdigest())

    assert found is not None
    assert (found.id, found.user_id) == (new.id, admin.id)
    assert missing is None


async def test_touch_sets_last_used_at_only_on_the_users_own_token(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyAdminTokensRepository(db_session_factory)
    admin = await _admin(db_session_factory, "tokens-touch")
    other = await _admin(db_session_factory, "tokens-touch-other")
    mine = await repository.add(_new(admin))
    theirs = await repository.add(_new(other))
    used = NOW + timedelta(hours=2)

    await repository.touch(admin.id, mine.id, at=used)
    await repository.touch(admin.id, theirs.id, at=used)

    [listed] = await repository.list_for_user(admin.id)
    [untouched] = await repository.list_for_user(other.id)
    assert listed.last_used_at == used
    assert untouched.last_used_at is None


async def test_touch_leaves_a_token_revoked_or_expired_meanwhile_unused(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """A revoke that lands between the lookup and the touch isn't followed by a later use."""
    repository = SqlAlchemyAdminTokensRepository(db_session_factory)
    admin = await _admin(db_session_factory, "tokens-touch-revoked")
    revoked = await repository.add(_new(admin))
    expiring = await repository.add(_new(admin, days=1))
    await repository.revoke(admin.id, revoked.id, at=NOW + timedelta(minutes=1))

    await repository.touch(admin.id, revoked.id, at=NOW + timedelta(minutes=2))
    await repository.touch(admin.id, expiring.id, at=NOW + timedelta(days=1))

    assert all(t.last_used_at is None for t in await repository.list_for_user(admin.id))
