import unicodedata
from collections.abc import Sequence
from uuid import UUID

from ai_trainer.application.ports.chat import ChatRepositoryPort, ConversationRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.domain.chat import ChatMessage, ChatRole, Conversation, conversation_title

MAX_MESSAGE_LENGTH = 4000
HISTORY_LIMIT = 50
CONVERSATION_LIST_LIMIT = 100


class EmptyMessageError(Exception):
    """Raised when a message has no text once surrounding whitespace is removed."""


class MessageTooLongError(Exception):
    """Raised when a message is longer than `MAX_MESSAGE_LENGTH` characters."""


class ConversationNotFoundError(Exception):
    """Raised for a conversation that doesn't exist or isn't the user's (ADR-0021)."""


def _has_visible_text(text: str) -> bool:
    """Whether any character is neither a separator nor a control or format character, so a
    message of zero-width spaces counts as empty."""
    return any(unicodedata.category(char)[0] not in "ZC" for char in text)


async def open_conversation(
    user_id: UUID, conversation_id: UUID, *, conversations: ConversationRepositoryPort
) -> Conversation:
    """The athlete's own conversation; another user's or an unknown one is not found (ADR-0021)."""
    conversation = await conversations.get(user_id, conversation_id)
    if conversation is None:
        raise ConversationNotFoundError
    return conversation


async def latest_conversation(
    user_id: UUID, *, conversations: ConversationRepositoryPort
) -> Conversation | None:
    """The conversation the athlete was last active in, which `/` opens; `None` before their
    first message (ADR-0021)."""
    recent = await conversations.list_recent(user_id, 1)
    return recent[0] if recent else None


async def list_conversations(
    user_id: UUID, *, conversations: ConversationRepositoryPort
) -> Sequence[Conversation]:
    """The athlete's conversations inside the retention window, latest activity first, for the
    conversation list (B25, F17). The window bounds it; the cap only guards the page."""
    return await conversations.list_recent(user_id, CONVERSATION_LIST_LIMIT)


async def send_message(
    user_id: UUID,
    conversation_id: UUID | None,
    text: str,
    *,
    clock: ClockPort,
    chat: ChatRepositoryPort,
    conversations: ConversationRepositoryPort,
) -> ChatMessage:
    """Stores the athlete's message in their conversation `conversation_id` and moves its last
    activity forward (PRD-0004 F1, B24). With no conversation, the message starts one, titled
    from it (ADR-0021). Browsers submit line breaks as CRLF, so they are stored as LF; the limit
    counts the trimmed text, and a rejected message stores nothing."""
    trimmed = text.replace("\r\n", "\n").strip()
    if not _has_visible_text(trimmed):
        raise EmptyMessageError
    if len(trimmed) > MAX_MESSAGE_LENGTH:
        raise MessageTooLongError
    now = clock.now()
    if conversation_id is None:
        conversation = await conversations.create(user_id, conversation_title(trimmed), now)
    else:
        conversation = await open_conversation(
            user_id, conversation_id, conversations=conversations
        )
    message = await chat.add(user_id, conversation.id, ChatRole.USER, trimmed, now)
    await conversations.touch(user_id, conversation.id, now)
    return message


async def list_history(
    user_id: UUID, conversation_id: UUID, *, repository: ChatRepositoryPort
) -> Sequence[ChatMessage]:
    """The conversation's latest `HISTORY_LIMIT` messages, oldest first (PRD-0004 B13)."""
    return await repository.list_recent(user_id, conversation_id, HISTORY_LIMIT)
