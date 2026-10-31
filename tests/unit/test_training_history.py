"""The training-history use case behind `query_sessions` (ADR-0010, ticket #378): it validates
the query, reads the user's sessions through the port and reports a domain failure as a
value, never a raise."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ai_trainer.application.training_history import (
    SessionPage,
    SessionQuery,
    SessionQueryFailed,
    TrainingHistory,
)
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.training import SessionSummary

_USER = uuid4()


def _summary(day: date, sport_id: str = "climbing") -> SessionSummary:
    return SessionSummary(
        id=uuid4(),
        sport_id=sport_id,
        occurred_on=day,
        duration_min=60,
        rpe=7,
        feeling=None,
        notes=None,
    )


@dataclass
class _Call:
    user_id: UUID
    sport_id: str | None
    since: date | None
    until: date | None
    limit: int


class _FakeHistory:
    """Implements `SessionHistoryPort` over an in-memory list per user."""

    def __init__(self, rows: dict[UUID, list[SessionSummary]]) -> None:
        self._rows = rows
        self.calls: list[_Call] = []

    async def list_sessions(
        self,
        user_id: UUID,
        *,
        sport_id: str | None,
        since: date | None,
        until: date | None,
        limit: int,
    ) -> Sequence[SessionSummary]:
        self.calls.append(_Call(user_id, sport_id, since, until, limit))
        rows = sorted(self._rows.get(user_id, []), key=lambda s: s.occurred_on, reverse=True)
        return [
            s
            for s in rows
            if (sport_id is None or s.sport_id == sport_id)
            and (since is None or s.occurred_on >= since)
            and (until is None or s.occurred_on <= until)
        ][:limit]


def _history(rows: dict[UUID, list[SessionSummary]]) -> tuple[TrainingHistory, _FakeHistory]:
    fake = _FakeHistory(rows)
    return TrainingHistory(fake, default_sport_registry()), fake


async def test_sessions_come_back_newest_day_first() -> None:
    older, newer = _summary(date(2026, 10, 1)), _summary(date(2026, 10, 5))
    history, _ = _history({_USER: [older, newer]})

    outcome = await history.query(_USER, SessionQuery())

    assert outcome == SessionPage(sessions=[newer, older], truncated=False)


async def test_a_page_past_the_limit_says_it_was_cut() -> None:
    rows = [_summary(date(2026, 10, day)) for day in range(1, 5)]
    history, fake = _history({_USER: rows})

    outcome = await history.query(_USER, SessionQuery(limit=3))

    assert isinstance(outcome, SessionPage)
    assert len(outcome.sessions) == 3
    assert outcome.truncated is True
    # One row past the limit tells a cut page from an exact one.
    assert fake.calls[0].limit == 4


async def test_filters_reach_the_port_for_the_given_user_only() -> None:
    history, fake = _history({_USER: [_summary(date(2026, 10, 3))]})

    await history.query(
        _USER,
        SessionQuery(sport_id="climbing", since=date(2026, 10, 1), until=date(2026, 10, 4)),
    )

    assert fake.calls == [_Call(_USER, "climbing", date(2026, 10, 1), date(2026, 10, 4), 11)]


async def test_an_unknown_sport_fails_with_the_known_sports_and_reads_nothing() -> None:
    history, fake = _history({_USER: [_summary(date(2026, 10, 3))]})

    outcome = await history.query(_USER, SessionQuery(sport_id="curling"))

    assert isinstance(outcome, SessionQueryFailed)
    assert outcome.reason == "unknown_sport"
    assert set(outcome.known_sports) == {p.id for p in default_sport_registry().all()}
    assert fake.calls == []


async def test_since_after_until_fails_and_reads_nothing() -> None:
    history, fake = _history({_USER: []})

    outcome = await history.query(
        _USER, SessionQuery(since=date(2026, 10, 5), until=date(2026, 10, 1))
    )

    assert outcome == SessionQueryFailed(reason="since_after_until")
    assert fake.calls == []


async def test_no_matching_session_is_a_failure_not_an_empty_page() -> None:
    history, _ = _history({uuid4(): [_summary(date(2026, 10, 3))]})

    outcome = await history.query(_USER, SessionQuery())

    assert outcome == SessionQueryFailed(reason="no_sessions")


@pytest.mark.parametrize("limit", [0, 51], ids=["zero", "over-max"])
def test_the_limit_is_bounded(limit: int) -> None:
    with pytest.raises(ValidationError):
        SessionQuery(limit=limit)
