"""The History page's read model (PRD 0011 F3, B4, ticket #515): the athlete's saved gym
sessions, newest first, with the time of day in their timezone (ADR-0014)."""

from collections.abc import Sequence
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ai_trainer.application.ports.gym_history import GymSessionReaderPort
from ai_trainer.domain.history import HistorySession, StoredGymSession


def _local(stored: StoredGymSession, timezone: ZoneInfo) -> HistorySession:
    first = stored.span.first_logged_at.astimezone(timezone)
    return HistorySession(
        id=stored.span.id,
        day=stored.span.occurred_on,
        time=first.time().replace(microsecond=0),
        exercises=stored.exercises,
        labels=stored.labels,
    )


def _zone(name: str) -> ZoneInfo:
    """The athlete's zone, UTC when the stored name is unknown (ADR-0014)."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


class GymSessionHistory:
    def __init__(self, reader: GymSessionReaderPort) -> None:
        self._reader = reader

    async def list(self, user_id: UUID, timezone: str) -> Sequence[HistorySession]:
        """The user's sessions, the latest day first, and within a day the latest start."""
        stored = await self._reader.list_sessions(user_id)
        ordered = sorted(
            stored,
            key=lambda session: (session.span.occurred_on, session.span.first_logged_at),
            reverse=True,
        )
        zone = _zone(timezone)
        return [_local(session, zone) for session in ordered]
