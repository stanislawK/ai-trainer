from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
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


async def test_a_stored_message_is_listed_back_with_its_role_text_and_time(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id = await _user(db_session_factory, "chat-a")

    stored = await repository.add(user_id, ChatRole.USER, "hello", _START)

    assert await repository.list_recent(user_id, 50) == [stored]
    assert (stored.role, stored.text, stored.created_at) == (ChatRole.USER, "hello", _START)


async def test_list_recent_returns_the_latest_messages_oldest_first(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id = await _user(db_session_factory, "chat-b")
    for index in range(7):
        await repository.add(user_id, ChatRole.USER, f"m{index}", _START + timedelta(minutes=index))

    recent = await repository.list_recent(user_id, 3)

    assert [message.text for message in recent] == ["m4", "m5", "m6"]


async def test_list_recent_never_returns_another_users_messages(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_a = await _user(db_session_factory, "chat-c")
    user_b = await _user(db_session_factory, "chat-d")
    await repository.add(user_a, ChatRole.USER, "for a only", _START)

    assert await repository.list_recent(user_b, 50) == []


async def test_deleting_a_user_cascades_their_chat_messages(db_session: AsyncSession) -> None:
    user_id = (
        await db_session.execute(
            text(
                "INSERT INTO users (id, sub, email) VALUES (gen_random_uuid(), 'chat-e', 'e@x.io')"
                " RETURNING id"
            )
        )
    ).scalar_one()
    await db_session.execute(
        text(
            "INSERT INTO chat_messages (id, user_id, role, text) "
            "VALUES (gen_random_uuid(), :user_id, 'user', 'hello')"
        ),
        {"user_id": user_id},
    )
    await db_session.commit()

    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    await db_session.commit()

    remaining = await db_session.execute(
        text("SELECT count(*) FROM chat_messages WHERE user_id = :id"), {"id": user_id}
    )
    assert remaining.scalar_one() == 0


async def test_a_reply_is_stored_with_its_sports_and_template(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id = await _user(db_session_factory, "chat-f")

    stored = await repository.add(
        user_id,
        ChatRole.ASSISTANT,
        "Not yet!",
        _START,
        sports=("cycling",),
        template_id="not_yet",
        template_version=1,
    )

    assert await repository.list_recent(user_id, 50) == [stored]
    assert (stored.sports, stored.template_id, stored.template_version) == (
        ("cycling",),
        "not_yet",
        1,
    )


async def test_get_finds_only_the_owners_message(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    owner = await _user(db_session_factory, "chat-g")
    stranger = await _user(db_session_factory, "chat-h")
    stored = await repository.add(owner, ChatRole.USER, "hi!", _START)

    assert await repository.get(owner, stored.id) == stored
    assert await repository.get(stranger, stored.id) is None


async def test_list_before_returns_the_latest_earlier_messages_oldest_first(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id = await _user(db_session_factory, "chat-i")
    other = await _user(db_session_factory, "chat-j")
    await repository.add(other, ChatRole.USER, "not mine", _START)
    stored = [
        await repository.add(user_id, ChatRole.USER, f"m{index}", _START + timedelta(minutes=index))
        for index in range(5)
    ]

    before = await repository.list_before(user_id, stored[3], 2)

    assert [message.text for message in before] == ["m1", "m2"]


async def test_list_replies_returns_the_answers_up_to_the_next_message(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id = await _user(db_session_factory, "chat-k")
    other = await _user(db_session_factory, "chat-l")
    asked = await repository.add(user_id, ChatRole.USER, "hi!", _START)
    first = await repository.add(
        user_id, ChatRole.ASSISTANT, "part 1", _START + timedelta(seconds=1)
    )
    second = await repository.add(
        user_id, ChatRole.ASSISTANT, "part 2", _START + timedelta(seconds=2)
    )
    await repository.add(other, ChatRole.ASSISTANT, "not mine", _START + timedelta(seconds=3))
    await repository.add(user_id, ChatRole.USER, "next", _START + timedelta(seconds=4))
    await repository.add(user_id, ChatRole.ASSISTANT, "later", _START + timedelta(seconds=5))

    assert await repository.list_replies(user_id, asked) == [first, second]


async def test_list_replies_is_empty_for_an_unanswered_message(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    repository = SqlAlchemyChatRepository(db_session_factory)
    user_id = await _user(db_session_factory, "chat-m")
    asked = await repository.add(user_id, ChatRole.USER, "hi!", _START)

    assert await repository.list_replies(user_id, asked) == []
