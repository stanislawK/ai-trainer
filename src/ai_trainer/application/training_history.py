"""Reading the athlete's saved sessions back (ADR-0010, ticket #378): the use case behind the
`query_sessions` MCP tool. A query that can't be answered comes back as a
`SessionQueryFailed` value, never a raise, so a tool can turn it into a recovery hint."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ai_trainer.application.ports.training import SessionHistoryPort
from ai_trainer.domain.sports.registry import SportRegistry, UnknownSportError
from ai_trainer.domain.training import SessionSummary

MAX_SESSIONS = 50


class SessionQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    sport_id: str | None = None
    since: date | None = None
    until: date | None = None
    limit: int = Field(default=10, ge=1, le=MAX_SESSIONS)


class SessionPage(BaseModel):
    """The sessions found, the latest day first; `truncated` when more matched than `limit`."""

    model_config = ConfigDict(frozen=True)

    sessions: list[SessionSummary]
    truncated: bool


type SessionQueryFailure = Literal["unknown_sport", "since_after_until", "no_sessions"]


class SessionQueryFailed(BaseModel):
    """Why a query found nothing to return; `known_sports` lists the sport IDs to pick from
    when the sport was unknown."""

    model_config = ConfigDict(frozen=True)

    reason: SessionQueryFailure
    known_sports: tuple[str, ...] = ()


class TrainingHistory:
    def __init__(self, sessions: SessionHistoryPort, sports: SportRegistry) -> None:
        self._sessions = sessions
        self._sports = sports

    async def query(self, user_id: UUID, query: SessionQuery) -> SessionPage | SessionQueryFailed:
        """The user's own sessions matching `query`. An unknown sport, a range that ends before
        it starts, or no match at all is a `SessionQueryFailed`."""
        if query.sport_id is not None:
            try:
                self._sports.get(query.sport_id)
            except UnknownSportError:
                return SessionQueryFailed(
                    reason="unknown_sport",
                    known_sports=tuple(plugin.id for plugin in self._sports.all()),
                )
        if query.since is not None and query.until is not None and query.since > query.until:
            return SessionQueryFailed(reason="since_after_until")
        # One row past the limit tells a cut page from an exact one.
        found = await self._sessions.list_sessions(
            user_id,
            sport_id=query.sport_id,
            since=query.since,
            until=query.until,
            limit=query.limit + 1,
        )
        if not found:
            return SessionQueryFailed(reason="no_sessions")
        return SessionPage(sessions=list(found[: query.limit]), truncated=len(found) > query.limit)
