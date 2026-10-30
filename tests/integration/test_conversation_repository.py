"""`SqlAlchemyConversationRepository` on PostgreSQL (ADR-0021, ticket #146): conversations are
per user, listed by last activity, touched forward only, and gone with their account."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.onboarding_repository import SqlAlchemyOnboardingRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.users import NewUser, UserStatus

_START = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
_RETENTION_DAYS = 14
# The purge is global and runs inside the test's transaction, so the purge tests live long
# before any row another test writes: it then matches and locks only their own rows, never one
# that a parallel worker commits or deletes (#328).
_PURGE_NOW = datetime(1990, 2, 1, 9, 0, tzinfo=UTC)
_PROFILE_TABLES = ("users", "user_sports", "weekly_availability", "goals")


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


def _repository(
    factory: Callable[[], AsyncSession], now: datetime = _START
) -> SqlAlchemyConversationRepository:
    return SqlAlchemyConversationRepository(
        factory, clock=_Clock(now), retention_days=_RETENTION_DAYS
    )


async def _user(factory: Callable[[], AsyncSession], sub: str) -> UUID:
    created = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    return created.id


async def test_a_created_conversation_is_found_with_its_title_and_times(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = _repository(db_session_factory)
    user_id = await _user(db_session_factory, "conv-a")

    created = await repository.create(user_id, "Rode 40 km", _START)

    assert await repository.get(user_id, created.id) == created
    assert (created.title, created.created_at, created.last_message_at) == (
        "Rode 40 km",
        _START,
        _START,
    )


async def test_get_finds_only_the_owners_conversation(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = _repository(db_session_factory)
    owner = await _user(db_session_factory, "conv-b")
    stranger = await _user(db_session_factory, "conv-c")
    created = await repository.create(owner, None, _START)

    assert await repository.get(stranger, created.id) is None
    assert await repository.get(owner, uuid4()) is None


async def test_list_recent_is_the_users_conversations_latest_activity_first(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = _repository(db_session_factory)
    user_id = await _user(db_session_factory, "conv-d")
    other = await _user(db_session_factory, "conv-e")
    oldest = await repository.create(user_id, "oldest", _START)
    middle = await repository.create(user_id, "middle", _START + timedelta(hours=1))
    newest = await repository.create(user_id, "newest", _START + timedelta(hours=2))
    await repository.create(other, "not mine", _START + timedelta(hours=3))

    recent = await repository.list_recent(user_id, 2)

    assert [conversation.id for conversation in recent] == [newest.id, middle.id]
    assert oldest.id not in {conversation.id for conversation in recent}


async def test_touch_moves_last_activity_forward_and_reorders_the_list(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = _repository(db_session_factory)
    user_id = await _user(db_session_factory, "conv-f")
    older = await repository.create(user_id, "older", _START)
    await repository.create(user_id, "newer", _START + timedelta(hours=1))
    later = _START + timedelta(hours=2)

    await repository.touch(user_id, older.id, later)

    [latest] = await repository.list_recent(user_id, 1)
    assert (latest.id, latest.last_message_at) == (older.id, later)


async def test_touch_never_moves_last_activity_back(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = _repository(db_session_factory)
    user_id = await _user(db_session_factory, "conv-g")
    created = await repository.create(user_id, None, _START)

    await repository.touch(user_id, created.id, _START - timedelta(minutes=1))

    assert await repository.get(user_id, created.id) == created


async def test_touch_never_changes_another_users_conversation(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = _repository(db_session_factory)
    owner = await _user(db_session_factory, "conv-h")
    stranger = await _user(db_session_factory, "conv-i")
    created = await repository.create(owner, None, _START)

    await repository.touch(stranger, created.id, _START + timedelta(days=1))

    assert await repository.get(owner, created.id) == created


async def test_deleting_a_conversation_cascades_its_messages_only(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    conversations = _repository(db_session_factory)
    chat = SqlAlchemyChatRepository(db_session_factory)
    user_id = await _user(db_session_factory, "conv-j")
    gone = await conversations.create(user_id, None, _START)
    kept = await conversations.create(user_id, None, _START)
    await chat.add(user_id, gone.id, ChatRole.USER, "gone", _START)
    stays = await chat.add(user_id, kept.id, ChatRole.USER, "stays", _START)

    async with db_session_factory() as db:
        await db.execute(text("DELETE FROM conversations WHERE id = :id"), {"id": gone.id})
        await db.commit()

    assert await chat.list_recent(user_id, gone.id, 50) == []
    assert await chat.list_recent(user_id, kept.id, 50) == [stays]


# Retention (PRD-0004 B26, ADR-0021, ticket #148).


async def test_a_conversation_exactly_at_the_retention_age_is_still_found_and_listed(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _user(db_session_factory, "ret-a")
    created = await _repository(db_session_factory).create(user_id, "kept", _START)
    on_the_boundary = _repository(db_session_factory, _START + timedelta(days=_RETENTION_DAYS))

    assert await on_the_boundary.get(user_id, created.id) == created
    assert await on_the_boundary.list_recent(user_id, 10) == [created]


async def test_a_conversation_a_second_past_the_retention_age_is_neither_found_nor_listed(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _user(db_session_factory, "ret-b")
    created = await _repository(db_session_factory).create(user_id, "expired", _START)
    past = _repository(db_session_factory, _START + timedelta(days=_RETENTION_DAYS, seconds=1))

    assert await past.get(user_id, created.id) is None
    assert await past.list_recent(user_id, 10) == []


async def test_list_recent_skips_expired_conversations_but_keeps_the_live_ones(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _user(db_session_factory, "ret-c")
    now = _START + timedelta(days=20)
    repository = _repository(db_session_factory, now)
    await repository.create(user_id, "old", _START)
    live = await repository.create(user_id, "live", now - timedelta(days=1))

    assert await repository.list_recent(user_id, 10) == [live]


async def _count(factory: Callable[[], AsyncSession], table: str, users: list[UUID]) -> int:
    """Rows of `table` that belong to `users`: other workers commit rows of their own (#328)."""
    column = "id" if table == "users" else "user_id"
    async with factory() as db:
        query = text(f"SELECT count(*) FROM {table} WHERE {column} = ANY(:users)")
        return int(await db.scalar(query, {"users": users}) or 0)


async def test_purge_deletes_only_expired_conversations_of_every_user_and_returns_the_count(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    now = _PURGE_NOW
    repository = _repository(db_session_factory, now)
    chat = SqlAlchemyChatRepository(db_session_factory)
    first = await _user(db_session_factory, "ret-d")
    second = await _user(db_session_factory, "ret-e")
    expired_first = await repository.create(first, "old a", now - timedelta(days=15))
    expired_second = await repository.create(second, "old b", now - timedelta(days=40))
    boundary = await repository.create(first, "boundary", now - timedelta(days=_RETENTION_DAYS))
    live = await repository.create(second, "live", now - timedelta(days=1))
    await chat.add(first, expired_first.id, ChatRole.USER, "gone", now - timedelta(days=15))
    await chat.add(second, expired_second.id, ChatRole.USER, "gone too", now - timedelta(days=40))
    kept_message = await chat.add(second, live.id, ChatRole.USER, "stays", now)

    deleted = await repository.purge_before(now - timedelta(days=_RETENTION_DAYS))

    assert deleted == 2
    assert {c.id for c in await repository.list_recent(first, 10)} == {boundary.id}
    assert await repository.list_recent(second, 10) == [live]
    assert await chat.list_recent(second, live.id, 50) == [kept_message]
    assert await _count(db_session_factory, "chat_messages", [first, second]) == 1


async def test_a_second_purge_deletes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    now = _PURGE_NOW
    repository = _repository(db_session_factory, now)
    cutoff = now - timedelta(days=_RETENTION_DAYS)
    user_id = await _user(db_session_factory, "ret-f")
    await repository.create(user_id, "old", now - timedelta(days=15))

    assert await repository.purge_before(cutoff) == 1
    assert await repository.purge_before(cutoff) == 0


async def test_purge_leaves_goals_and_profile_rows_untouched(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    now = _PURGE_NOW
    repository = _repository(db_session_factory, now)
    onboarding = SqlAlchemyOnboardingRepository(db_session_factory)
    user_id = await _user(db_session_factory, "ret-g")
    await onboarding.replace_sports(user_id, ["climbing"])
    # `weekly_availability` is no longer written (ADR-0028), but its rows stay until the
    # contract ticket drops the table.
    async with db_session_factory() as session:
        await session.execute(
            text("INSERT INTO weekly_availability (user_id, weekday, minutes) VALUES (:u, 0, 60)"),
            {"u": user_id},
        )
        await session.commit()
    await onboarding.replace_goals(user_id, [Goal(text="Send 8a+ by spring")])
    await onboarding.set_onboarded_at(user_id, now)
    await repository.create(user_id, "old", now - timedelta(days=15))
    before = [await _count(db_session_factory, table, [user_id]) for table in _PROFILE_TABLES]

    await repository.purge_before(now - timedelta(days=_RETENTION_DAYS))

    assert [
        await _count(db_session_factory, table, [user_id]) for table in _PROFILE_TABLES
    ] == before
    assert all(count >= 1 for count in before)
    assert await onboarding.list_sports(user_id) == ["climbing"]
    assert await onboarding.list_goals(user_id) == [Goal(text="Send 8a+ by spring")]
