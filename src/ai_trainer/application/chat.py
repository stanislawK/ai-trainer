import unicodedata
from collections.abc import Sequence
from uuid import UUID

from ai_trainer.application.ports.chat import ChatRepositoryPort
from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.domain.chat import ChatMessage, ChatRole

MAX_MESSAGE_LENGTH = 4000
HISTORY_LIMIT = 50


class EmptyMessageError(Exception):
    """Raised when a message has no text once surrounding whitespace is removed."""


class MessageTooLongError(Exception):
    """Raised when a message is longer than `MAX_MESSAGE_LENGTH` characters."""


def _has_visible_text(text: str) -> bool:
    """Whether any character is neither a separator nor a control or format character, so a
    message of zero-width spaces counts as empty."""
    return any(unicodedata.category(char)[0] not in "ZC" for char in text)


async def send_message(
    user_id: UUID, text: str, *, clock: ClockPort, repository: ChatRepositoryPort
) -> ChatMessage:
    """Stores the athlete's message (PRD-0003 F1). Browsers submit line breaks as CRLF, so they
    are stored as LF; the limit counts the trimmed text, and a rejected message stores nothing."""
    trimmed = text.replace("\r\n", "\n").strip()
    if not _has_visible_text(trimmed):
        raise EmptyMessageError
    if len(trimmed) > MAX_MESSAGE_LENGTH:
        raise MessageTooLongError
    return await repository.add(user_id, ChatRole.USER, trimmed, clock.now())


async def list_history(user_id: UUID, *, repository: ChatRepositoryPort) -> Sequence[ChatMessage]:
    """The athlete's latest `HISTORY_LIMIT` messages, oldest first (PRD-0003 B13)."""
    return await repository.list_recent(user_id, HISTORY_LIMIT)
