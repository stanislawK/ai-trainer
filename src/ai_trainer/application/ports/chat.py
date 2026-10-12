from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_trainer.domain.chat import ChatMessage, ChatRole, Conversation


class ChatRepositoryPort(Protocol):
    """Persists an athlete's chat (ADR-0008, ADR-0021). Every method is scoped to one `user_id`,
    which comes from the authenticated session (ADR-0005), and every list stays inside one
    conversation."""

    async def add(
        self,
        user_id: UUID,
        conversation_id: UUID,
        role: ChatRole,
        text: str,
        created_at: datetime,
        *,
        sports: Sequence[str] = (),
        template_id: str | None = None,
        template_version: int | None = None,
    ) -> ChatMessage:
        """Stores a message in the user's conversation `conversation_id`, which the caller has
        already looked up with the same `user_id`."""
        ...

    async def get(self, user_id: UUID, message_id: UUID) -> ChatMessage | None:
        """The user's message with this ID; `None` if it doesn't exist or isn't theirs."""
        ...

    async def list_recent(
        self, user_id: UUID, conversation_id: UUID, limit: int
    ) -> Sequence[ChatMessage]:
        """The latest `limit` messages of the user's conversation, oldest first."""
        ...

    async def list_before(
        self, user_id: UUID, before: ChatMessage, limit: int
    ) -> Sequence[ChatMessage]:
        """The latest `limit` messages older than `before` in its conversation, oldest first."""
        ...

    async def list_replies(self, user_id: UUID, to: ChatMessage) -> Sequence[ChatMessage]:
        """The assistant messages after `to` in its conversation, up to the user's next message,
        oldest first."""
        ...


class ConversationRepositoryPort(Protocol):
    """Persists an athlete's conversations (ADR-0021). Every method is scoped to one `user_id`
    from the authenticated session, so another user's conversation is never found."""

    async def create(self, user_id: UUID, title: str | None, now: datetime) -> Conversation:
        """A new conversation, created and last active at `now`."""
        ...

    async def get(self, user_id: UUID, conversation_id: UUID) -> Conversation | None:
        """The user's conversation with this ID; `None` if it doesn't exist or isn't theirs."""
        ...

    async def list_recent(self, user_id: UUID, limit: int) -> Sequence[Conversation]:
        """The user's `limit` most recently active conversations, latest first."""
        ...

    async def touch(self, user_id: UUID, conversation_id: UUID, when: datetime) -> None:
        """Moves the conversation's `last_message_at` forward to `when`, never back."""
        ...
