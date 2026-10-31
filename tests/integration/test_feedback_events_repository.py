"""`feedback_events` (ADR-0034, PRD-0010 G6/G7, ticket #394): content-free rows that belong to
their user, go with the account, and are read for the report without who made them."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.feedback_repository import SqlAlchemyFeedbackEventsRepository
from ai_trainer.application.drafts import edit_draft
from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.domain.feedback import FeedbackKind, NewFeedbackEvent
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.training import SessionDraft
from tests.chat_support import FROZEN_NOW, FixedClock, InMemoryDraftRepository

_INSERT_USER = text("INSERT INTO users (id, sub, email) VALUES (:id, :sub, :email)")
_DELETE_USER = text("DELETE FROM users WHERE id = :id")
_COUNT = text("SELECT count(*) FROM feedback_events WHERE user_id = :user_id")
_NOW = datetime(2026, 10, 31, 9, 30, tzinfo=UTC)
_TYPED = "dropped the bar on my foot at 187 kg"


async def _user(session_factory: Callable[[], AsyncSession]) -> UUID:
    user_id = uuid4()
    async with session_factory() as session:
        await session.execute(
            _INSERT_USER, {"id": user_id, "sub": str(user_id), "email": f"{user_id}@example.com"}
        )
        await session.commit()
    return user_id


def _event(user_id: UUID, **overrides: object) -> NewFeedbackEvent:
    values: dict[str, object] = {
        "user_id": user_id,
        "kind": FeedbackKind.DRAFT_EDITED,
        "template_id": "gym.extract",
        "template_version": 3,
        "fields": ["reps"],
        "created_at": _NOW,
    }
    values.update(overrides)
    return NewFeedbackEvent.model_validate(values)


async def test_a_recorded_event_reads_back_with_its_fields(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _user(db_session_factory)
    repository = SqlAlchemyFeedbackEventsRepository(db_session_factory)

    recorded = await repository.record(_event(user_id))

    [read] = await repository.list_for_user(user_id)
    assert read == recorded
    assert (read.kind, read.template_id, read.template_version) == (
        FeedbackKind.DRAFT_EDITED,
        "gym.extract",
        3,
    )
    assert (tuple(read.fields), read.created_at) == (("reps",), _NOW)


async def test_an_event_with_no_fields_and_no_template_reads_back_empty(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _user(db_session_factory)
    repository = SqlAlchemyFeedbackEventsRepository(db_session_factory)

    await repository.record(
        _event(
            user_id,
            kind=FeedbackKind.DRAFT_DISCARDED,
            template_id=None,
            template_version=None,
            fields=[],
        )
    )

    [read] = await repository.list_for_user(user_id)
    assert (read.template_id, read.template_version, tuple(read.fields)) == (None, None, ())


async def test_a_decision_overridden_row_reads_back_so_the_report_never_crashes(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _user(db_session_factory)
    repository = SqlAlchemyFeedbackEventsRepository(db_session_factory)
    await repository.record(_event(user_id, kind=FeedbackKind.DECISION_OVERRIDDEN, fields=[]))

    [read] = await repository.list_for_user(user_id)
    signals = await repository.signals()

    assert read.kind is FeedbackKind.DECISION_OVERRIDDEN
    assert FeedbackKind.DECISION_OVERRIDDEN in {signal.kind for signal in signals}


async def test_another_users_events_are_never_read(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    mine, theirs = await _user(db_session_factory), await _user(db_session_factory)
    repository = SqlAlchemyFeedbackEventsRepository(db_session_factory)
    await repository.record(_event(theirs, fields=["sets"]))

    assert await repository.list_for_user(mine) == []


async def test_deleting_the_account_removes_its_events_and_only_its_events(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    gone, kept = await _user(db_session_factory), await _user(db_session_factory)
    repository = SqlAlchemyFeedbackEventsRepository(db_session_factory)
    await repository.record(_event(gone))
    await repository.record(_event(kept))

    async with db_session_factory() as session:
        await session.execute(_DELETE_USER, {"id": gone})
        await session.commit()
        assert (await session.execute(_COUNT, {"user_id": gone})).scalar_one() == 0
        assert (await session.execute(_COUNT, {"user_id": kept})).scalar_one() == 1


async def test_signals_carry_no_user_and_honour_since(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _user(db_session_factory)
    repository = SqlAlchemyFeedbackEventsRepository(db_session_factory)
    await repository.record(_event(user_id, created_at=_NOW - timedelta(days=40), fields=["old"]))
    await repository.record(_event(user_id, fields=["new"]))

    recent = [
        s for s in await repository.signals(since=_NOW - timedelta(days=1)) if "new" in s.fields
    ]
    everything = await repository.signals()

    assert [tuple(s.fields) for s in recent] == [("new",)]
    assert {"old", "new"} <= {name for s in everything for name in s.fields}
    assert not hasattr(everything[0], "user_id")


async def test_no_column_holds_message_text_or_a_typed_value(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user_id = await _user(db_session_factory)
    repository = SqlAlchemyFeedbackEventsRepository(db_session_factory)
    # A real edit that types text into the exercise name, recorded into the real table.
    drafts = InMemoryDraftRepository()
    draft = await drafts.add(
        user_id,
        uuid4(),
        uuid4(),
        "gym",
        SessionDraft(
            occurred_on=FROZEN_NOW.date(),
            date_source="assumed_today",
            timezone="Europe/Warsaw",
            payload={
                "sport": "gym",
                "activities": [{"name": "squat", "sets": 5, "reps": 5, "load": None, "rir": None}],
            },
        ),
        payload_version=1,
        created_at=FROZEN_NOW,
        template_id="gym.extract",
        template_version=3,
    )

    await edit_draft(
        user_id,
        draft.id,
        {
            "occurred_on": FROZEN_NOW.date().isoformat(),
            "duration_min": "",
            "rpe": "",
            "feeling": "",
            "activities": [
                {"name": _TYPED, "sets": "5", "reps": "5", "load": "", "rir": ""},
            ],
        },
        drafts=drafts,
        sports=default_sport_registry(),
        feedback=FeedbackRecorder(repository, clock=FixedClock()),
    )

    async with db_session_factory() as session:
        columns = (
            (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'feedback_events'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert set(columns) == {
            "id",
            "user_id",
            "kind",
            "template_id",
            "template_version",
            "fields",
            "created_at",
        }
        # Every column of the row, rendered as text, holds none of what the athlete typed.
        row = (
            await session.execute(
                text(
                    "SELECT to_jsonb(feedback_events)::text FROM feedback_events WHERE user_id = :u"
                ),
                {"u": user_id},
            )
        ).scalar_one()
    assert _TYPED not in row
    assert "name" in row  # the row exists and names the field that changed
    [stored] = await repository.list_for_user(user_id)
    assert (stored.kind, tuple(stored.fields)) == (FeedbackKind.DRAFT_EDITED, ("name_as_typed",))
