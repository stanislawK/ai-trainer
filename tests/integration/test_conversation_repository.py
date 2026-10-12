"""`SqlAlchemyConversationRepository` on PostgreSQL (ADR-0021, ticket #146): conversations are
per user, listed by last activity, touched forward only, and gone with their account."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.users import NewUser, UserStatus

_START = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)


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
    repository = SqlAlchemyConversationRepository(db_session_factory)
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
    repository = SqlAlchemyConversationRepository(db_session_factory)
    owner = await _user(db_session_factory, "conv-b")
    stranger = await _user(db_session_factory, "conv-c")
    created = await repository.create(owner, None, _START)

    assert await repository.get(stranger, created.id) is None
    assert await repository.get(owner, uuid4()) is None


async def test_list_recent_is_the_users_conversations_latest_activity_first(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyConversationRepository(db_session_factory)
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
    repository = SqlAlchemyConversationRepository(db_session_factory)
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
    repository = SqlAlchemyConversationRepository(db_session_factory)
    user_id = await _user(db_session_factory, "conv-g")
    created = await repository.create(user_id, None, _START)

    await repository.touch(user_id, created.id, _START - timedelta(minutes=1))

    assert await repository.get(user_id, created.id) == created


async def test_touch_never_changes_another_users_conversation(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyConversationRepository(db_session_factory)
    owner = await _user(db_session_factory, "conv-h")
    stranger = await _user(db_session_factory, "conv-i")
    created = await repository.create(owner, None, _START)

    await repository.touch(stranger, created.id, _START + timedelta(days=1))

    assert await repository.get(owner, created.id) == created


async def test_deleting_a_conversation_cascades_its_messages_only(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    conversations = SqlAlchemyConversationRepository(db_session_factory)
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
