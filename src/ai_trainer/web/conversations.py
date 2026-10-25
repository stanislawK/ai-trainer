"""The conversation list's rows (PRD-0008 B25, F17, ADR-0021, ticket #213): each conversation
with which kind of last-activity label it gets in the athlete's timezone (ADR-0014). The words
of the label (Yesterday, weekday and month names) live in `partials/chat/conversations.html`
(G5); this only decides the kind."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from ai_trainer.domain.chat import Conversation

type LastActiveKind = Literal["today", "yesterday", "weekday", "date"]


@dataclass(frozen=True, slots=True)
class LastActive:
    """Mock "Conversation list v3": today's HH:MM, then Yesterday, the weekday within this
    Monday-to-Sunday week, then the day and month. `local` is the time in the athlete's zone."""

    kind: LastActiveKind
    local: datetime


@dataclass(frozen=True, slots=True)
class ConversationRow:
    id: UUID
    title: str | None
    last_active: LastActive


def last_active(when: datetime, *, now: datetime, timezone: str) -> LastActive:
    zone = ZoneInfo(timezone)
    local = when.astimezone(zone)
    today = now.astimezone(zone).date()
    day = local.date()
    if day == today:
        kind: LastActiveKind = "today"
    elif day == today - timedelta(days=1):
        kind = "yesterday"
    elif day.isocalendar()[:2] == today.isocalendar()[:2]:
        kind = "weekday"
    else:
        kind = "date"
    return LastActive(kind, local)


def conversation_rows(
    conversations: Sequence[Conversation], *, now: datetime, timezone: str
) -> list[ConversationRow]:
    return [
        ConversationRow(
            conversation.id,
            conversation.title,
            last_active(conversation.last_message_at, now=now, timezone=timezone),
        )
        for conversation in conversations
    ]
