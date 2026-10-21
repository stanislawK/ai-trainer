"""Session continuity rules (PRD-0004 B16, ADR-0014, ticket #165): a log joins the athlete's
latest same-sport session when it was sent within the merge window of that session's latest log,
unless it names another day; joining appends its activities through the sport's payload model,
and taking it back removes exactly them."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import JsonValue, ValidationError

from ai_trainer.domain.sports.base import extend_payload, withdraw_payload
from ai_trainer.domain.sports.climbing.payload import ClimbingPayload
from ai_trainer.domain.training import (
    SessionDraft,
    SessionSpan,
    continuity,
    joins_day,
    joins_session,
)

_LAST_LOG = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)
_WINDOW = timedelta(minutes=30)
_SESSION = SessionSpan(
    id=uuid4(),
    occurred_on=date(2026, 10, 2),
    first_logged_at=_LAST_LOG - timedelta(minutes=20),
    last_logged_at=_LAST_LOG,
)
_SIX_B = {"sport": "climbing", "activities": [{"kind": "lead", "count": 1, "grade": None}]}


def _draft(occurred_on: date = date(2026, 10, 2), source: str = "assumed_today") -> SessionDraft:
    return SessionDraft.model_validate(
        {
            "occurred_on": occurred_on,
            "date_source": source,
            "timezone": "Europe/Warsaw",
            "payload": _SIX_B,
        }
    )


def test_a_log_sent_exactly_at_the_window_joins_and_one_second_later_does_not() -> None:
    assert joins_session(_draft(), _LAST_LOG + _WINDOW, _SESSION, _WINDOW)
    assert not joins_session(
        _draft(), _LAST_LOG + _WINDOW + timedelta(seconds=1), _SESSION, _WINDOW
    )


def test_the_window_is_measured_from_the_latest_log_not_the_first() -> None:
    sent = _SESSION.first_logged_at + _WINDOW + timedelta(minutes=5)

    assert joins_session(_draft(), sent, _SESSION, _WINDOW)


def test_a_log_a_day_later_does_not_join() -> None:
    assert not joins_session(
        _draft(date(2026, 10, 3)), _LAST_LOG + timedelta(days=1), _SESSION, _WINDOW
    )


@pytest.mark.parametrize("source", ["stated", "relative"])
def test_a_log_naming_another_day_never_joins_even_inside_the_window(source: str) -> None:
    yesterday = _draft(date(2026, 10, 1), source)

    assert not joins_day(yesterday, _SESSION.occurred_on)
    assert not joins_session(yesterday, _LAST_LOG + timedelta(minutes=5), _SESSION, _WINDOW)


def test_an_assumed_today_log_after_midnight_still_joins_the_evening_session() -> None:
    after_midnight = _draft(date(2026, 10, 3), "assumed_today")

    assert joins_session(after_midnight, _LAST_LOG + timedelta(minutes=10), _SESSION, _WINDOW)


def test_a_log_naming_the_sessions_own_day_joins() -> None:
    assert joins_day(_draft(date(2026, 10, 2), "stated"), _SESSION.occurred_on)


_SIX_A: dict[str, JsonValue] = {
    "kind": "boulder",
    "count": 3,
    "grade": {"original": "6A", "scale": "font"},
}
_SIX_B_PLUS: dict[str, JsonValue] = {
    "kind": "lead",
    "count": 1,
    "grade": {"original": "6b+", "scale": "french"},
}


def _payload(*activities: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return ClimbingPayload.model_validate({"activities": list(activities)}).model_dump(mode="json")


def test_extending_appends_the_additions_activities_after_the_sessions_own() -> None:
    extended = extend_payload(ClimbingPayload, _payload(_SIX_A), _payload(_SIX_B_PLUS))

    assert extended.model_dump(mode="json") == _payload(_SIX_A, _SIX_B_PLUS)


def test_extending_with_a_payload_its_model_rejects_raises() -> None:
    with pytest.raises(ValidationError):
        extend_payload(ClimbingPayload, _payload(_SIX_A), {"activities": []})


def test_withdrawing_removes_exactly_the_additions_activities_once_each() -> None:
    session = _payload(_SIX_A, _SIX_B_PLUS, _SIX_A)

    withdrawn = withdraw_payload(ClimbingPayload, session, _payload(_SIX_A))

    assert withdrawn.model_dump(mode="json") == _payload(_SIX_A, _SIX_B_PLUS)


def test_withdrawing_an_activity_the_session_lacks_leaves_the_rest() -> None:
    withdrawn = withdraw_payload(ClimbingPayload, _payload(_SIX_A), _payload(_SIX_B_PLUS))

    assert withdrawn.model_dump(mode="json") == _payload(_SIX_A)


# Beyond the window, on the session's own day, the athlete is asked (B16, ADR-0014, ticket
# #166). The day is the draft's resolved `occurred_on`, so a log sent after midnight about the
# evening before counts as the evening's day.

_EVENING = SessionSpan(
    id=uuid4(),
    occurred_on=date(2026, 10, 2),
    first_logged_at=datetime(2026, 10, 2, 19, 0, tzinfo=UTC),
    last_logged_at=datetime(2026, 10, 2, 19, 30, tzinfo=UTC),
)
# 00:10 in Warsaw (CEST, UTC+2) on 3 October.
_AFTER_MIDNIGHT = datetime(2026, 10, 2, 22, 10, tzinfo=UTC)


def test_a_log_within_the_window_joins_with_no_question() -> None:
    assert continuity(_draft(), _LAST_LOG + _WINDOW, _SESSION, _WINDOW) == "join"


def test_a_log_three_hours_later_on_the_same_day_is_asked_about() -> None:
    assert continuity(_draft(), _LAST_LOG + timedelta(hours=3), _SESSION, _WINDOW) == "ask"


def test_a_log_one_second_past_the_window_on_the_same_day_is_asked_about() -> None:
    just_past = _LAST_LOG + _WINDOW + timedelta(seconds=1)

    assert continuity(_draft(), just_past, _SESSION, _WINDOW) == "ask"


def test_a_log_on_another_local_day_starts_a_new_session_with_no_question() -> None:
    tomorrow = _draft(date(2026, 10, 3))

    assert continuity(tomorrow, _LAST_LOG + timedelta(days=1), _SESSION, _WINDOW) == "new"


@pytest.mark.parametrize("source", ["stated", "relative"])
def test_a_log_after_midnight_about_the_evening_before_counts_as_that_day(source: str) -> None:
    last_night = _draft(date(2026, 10, 2), source)

    assert continuity(last_night, _AFTER_MIDNIGHT, _EVENING, _WINDOW) == "ask"


def test_an_unstated_log_after_midnight_beyond_the_window_is_its_own_day() -> None:
    today = _draft(date(2026, 10, 3), "assumed_today")

    assert continuity(today, _AFTER_MIDNIGHT, _EVENING, _WINDOW) == "new"


def test_a_log_naming_another_day_inside_the_window_starts_a_new_session() -> None:
    yesterday = _draft(date(2026, 10, 1), "relative")

    assert continuity(yesterday, _LAST_LOG + timedelta(minutes=5), _SESSION, _WINDOW) == "new"
