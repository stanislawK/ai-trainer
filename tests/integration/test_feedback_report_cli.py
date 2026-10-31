"""`ai-trainer feedback-report` output and exit code (ADR-0034, ticket #394): counts per kind,
template and field, and "no feedback events" with exit 0 for an empty table. Rows hold no text,
so the report prints none."""

import io
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer import cli
from ai_trainer.adapters.feedback_repository import SqlAlchemyFeedbackEventsRepository
from ai_trainer.domain.feedback import FeedbackKind, NewFeedbackEvent

_NOW = datetime(2026, 10, 31, 9, 30, tzinfo=UTC)


async def _user(session_factory: Callable[[], AsyncSession]) -> UUID:
    user_id = uuid4()
    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO users (id, sub, email) VALUES (:id, :sub, :email)"),
            {"id": user_id, "sub": str(user_id), "email": f"{user_id}@example.com"},
        )
        await session.commit()
    return user_id


async def test_an_empty_table_prints_no_feedback_events_and_exits_zero(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    output = io.StringIO()
    async with db_session_factory() as session:
        await session.execute(text("DELETE FROM feedback_events"))
        await session.commit()

    code = await cli.feedback_report(
        events=SqlAlchemyFeedbackEventsRepository(db_session_factory), since=None, output=output
    )

    assert code == 0
    assert output.getvalue() == "no feedback events\n"


async def test_prints_a_count_per_kind_template_and_field(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    output = io.StringIO()
    user_id = await _user(db_session_factory)
    repository = SqlAlchemyFeedbackEventsRepository(db_session_factory)
    async with db_session_factory() as session:
        await session.execute(text("DELETE FROM feedback_events"))
        await session.commit()
    for fields in (["reps", "sets"], ["reps"]):
        await repository.record(
            NewFeedbackEvent(
                user_id=user_id,
                kind=FeedbackKind.DRAFT_EDITED,
                template_id="gym.extract",
                template_version=3,
                fields=fields,
                created_at=_NOW,
            )
        )
    await repository.record(
        NewFeedbackEvent(user_id=user_id, kind=FeedbackKind.DRAFT_DISCARDED, created_at=_NOW)
    )

    code = await cli.feedback_report(events=repository, since=None, output=output)

    assert code == 0
    assert output.getvalue().splitlines() == [
        "draft_edited gym.extract reps 2",
        "draft_discarded - - 1",
        "draft_edited gym.extract sets 1",
    ]
    assert str(user_id) not in output.getvalue()


async def test_since_leaves_out_older_events(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    output = io.StringIO()
    user_id = await _user(db_session_factory)
    repository = SqlAlchemyFeedbackEventsRepository(db_session_factory)
    async with db_session_factory() as session:
        await session.execute(text("DELETE FROM feedback_events"))
        await session.commit()
    await repository.record(
        NewFeedbackEvent(
            user_id=user_id, kind=FeedbackKind.DRAFT_DISCARDED, created_at=_NOW - timedelta(days=40)
        )
    )

    code = await cli.feedback_report(
        events=repository, since=_NOW - timedelta(days=1), output=output
    )

    assert (code, output.getvalue()) == (0, "no feedback events\n")


async def test_run_opens_its_own_engine_and_prints_the_report() -> None:
    output = io.StringIO()

    code = await cli.run(["feedback-report"], output=output)

    # Tests roll back, so the database holds no committed feedback event.
    assert (code, output.getvalue()) == (0, "no feedback events\n")


def test_since_is_parsed_as_an_aware_time_and_a_bad_one_is_refused() -> None:
    args = cli.parse_args(["feedback-report", "--since", "2026-10-01T00:00:00"])
    assert args.since == datetime(2026, 10, 1, tzinfo=UTC)

    with pytest.raises(SystemExit):
        cli.parse_args(["feedback-report", "--since", "last tuesday"])
