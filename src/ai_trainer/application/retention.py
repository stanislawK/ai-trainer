"""Conversation retention (PRD-0004 B26, ADR-0021): the purge that backs the read-time expiry."""

from datetime import timedelta

from ai_trainer.application.ports.chat import ConversationRepositoryPort
from ai_trainer.application.ports.clock import ClockPort


async def purge_expired_conversations(
    *, clock: ClockPort, conversations: ConversationRepositoryPort, retention_days: int
) -> int:
    """Hard-deletes every conversation last active more than `retention_days` ago, across
    users, and returns how many went. One exactly `retention_days` old stays, as it does on
    read. A second run finds nothing."""
    return await conversations.purge_before(clock.now() - timedelta(days=retention_days))
