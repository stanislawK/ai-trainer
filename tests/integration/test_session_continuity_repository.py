"""Session continuity on PostgreSQL (PRD-0004 B16, ADR-0014, ADR-0022, ticket #165): the
athlete's latest same-sport session is found by its latest log, a draft confirmed onto it adds
its activities and a source row, and an undo takes back only its own log."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.chat_repository import SqlAlchemyChatRepository
from ai_trainer.adapters.conversation_repository import SqlAlchemyConversationRepository
from ai_trainer.adapters.session_draft_repository import SqlAlchemySessionDraftRepository
from ai_trainer.adapters.training_session_repository import SqlAlchemyTrainingSessionRepository
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.domain.chat import ChatRole
from ai_trainer.domain.training import (
    DraftNotFoundError,
    SessionDraft,
    SessionNotFoundError,
    SessionSpan,
    StoredDraft,
)
from ai_trainer.domain.users import NewUser, UserStatus
from tests.activity_support import STORES

_START = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)
_DAY = date(2026, 10, 2)
_LATER = _START + timedelta(hours=1)


class _Clock:
    def now(self) -> datetime:
        return _START


def _climb(grade: str) -> dict[str, Any]:
    return {
        "sport": "climbing",
        "activities": [
            {"kind": "lead", "count": 1, "grade": {"original": grade, "scale": "french"}}
        ],
    }


_GYM = {"sport": "gym", "activities": [{"name": "Deadlift", "sets": 3, "reps": 5}]}


def _draft(payload: dict[str, Any], **envelope: Any) -> SessionDraft:
    return SessionDraft.model_validate(
        {
            "occurred_on": _DAY,
            "date_source": "assumed_today",
            "timezone": "Europe/Warsaw",
            "payload": payload,
            **envelope,
        }
    )


class _Athlete:
    """A user with one conversation, who logs and confirms through the real repositories."""

    def __init__(self, factory: Callable[[], AsyncSession], user_id: UUID, conversation_id: UUID):
        self.factory = factory
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.drafts = SqlAlchemySessionDraftRepository(factory, stores=STORES)

    async def draft(
        self,
        log: str,
        at: datetime,
        draft: SessionDraft,
        *,
        sport_id: str = "climbing",
        extends: UUID | None = None,
    ) -> StoredDraft:
        message = await SqlAlchemyChatRepository(self.factory).add(
            self.user_id, self.conversation_id, ChatRole.USER, log, at
        )
        return await self.drafts.add(
            self.user_id,
            self.conversation_id,
            message.id,
            sport_id,
            draft,
            payload_version=1,
            created_at=at,
            extends_session_id=extends,
        )

    async def confirmed(self, draft: StoredDraft, at: datetime | None = None) -> StoredDraft:
        return await self.drafts.confirm(
            self.user_id, draft.id, confirmed_at=at or draft.created_at
        )


async def _athlete(factory: Callable[[], AsyncSession], sub: str) -> _Athlete:
    user = await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )
    conversation = await SqlAlchemyConversationRepository(
        factory, clock=_Clock(), retention_days=14
    ).create(user.id, "Climbed", _START)
    return _Athlete(factory, user.id, conversation.id)


async def _session(factory: Callable[[], AsyncSession], session_id: UUID | None) -> Any:
    async with factory() as db:
        return (
            await db.execute(
                text(
                    "SELECT payload, duration_min, rpe, feeling, updated_at"
                    " FROM training_sessions WHERE id = :id"
                ),
                {"id": session_id},
            )
        ).one_or_none()


async def _sources(factory: Callable[[], AsyncSession], session_id: UUID | None) -> list[str]:
    async with factory() as db:
        rows = await db.scalars(
            text(
                "SELECT raw_text FROM training_session_sources WHERE session_id = :id"
                " ORDER BY logged_at"
            ),
            {"id": session_id},
        )
        return list(rows.all())


async def _session_count(factory: Callable[[], AsyncSession], user_id: UUID) -> int:
    async with factory() as db:
        count = await db.scalar(
            text("SELECT count(*) FROM training_sessions WHERE user_id = :id"), {"id": user_id}
        )
        return count or 0


def _grades(payload: dict[str, Any]) -> list[str]:
    return [activity["grade"]["original"] for activity in payload["activities"]]


# --- the latest same-sport session ---------------------------------------------------------


async def test_no_session_yet_has_no_latest(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "latest-none")

    latest = await SqlAlchemyTrainingSessionRepository(
        db_session_factory, stores=STORES
    ).latest_in_sport(athlete.user_id, "climbing")

    assert latest is None


async def test_the_latest_session_spans_its_first_and_latest_logs(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "latest-span")
    first = await athlete.confirmed(await athlete.draft("sent 6b", _START, _draft(_climb("6b"))))
    later = _START + timedelta(minutes=10)
    await athlete.confirmed(
        await athlete.draft("and a 6b+", later, _draft(_climb("6b+")), extends=first.session_id)
    )

    latest = await SqlAlchemyTrainingSessionRepository(
        db_session_factory, stores=STORES
    ).latest_in_sport(athlete.user_id, "climbing")

    assert first.session_id is not None
    assert latest == SessionSpan(
        id=first.session_id, occurred_on=_DAY, first_logged_at=_START, last_logged_at=later
    )


async def test_the_latest_session_is_the_one_with_the_latest_log_in_that_sport(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "latest-sport")
    await athlete.confirmed(await athlete.draft("sent 6a yesterday", _START, _draft(_climb("6a"))))
    climb = await athlete.confirmed(
        await athlete.draft("sent 6b", _START + timedelta(hours=2), _draft(_climb("6b")))
    )
    await athlete.confirmed(
        await athlete.draft("deadlifts", _START + timedelta(hours=3), _draft(_GYM), sport_id="gym")
    )

    latest = await SqlAlchemyTrainingSessionRepository(
        db_session_factory, stores=STORES
    ).latest_in_sport(athlete.user_id, "climbing")

    assert latest is not None
    assert latest.id == climb.session_id


async def test_another_users_sessions_are_never_the_latest(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    owner = await _athlete(db_session_factory, "latest-owner")
    stranger = await _athlete(db_session_factory, "latest-stranger")
    await owner.confirmed(await owner.draft("sent 6b", _START, _draft(_climb("6b"))))

    latest = await SqlAlchemyTrainingSessionRepository(
        db_session_factory, stores=STORES
    ).latest_in_sport(stranger.user_id, "climbing")

    assert latest is None


# --- confirming onto a session -------------------------------------------------------------


async def test_three_logs_minutes_apart_confirm_into_one_session_with_three_sources(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "extend-three")
    first = await athlete.confirmed(await athlete.draft("sent 6b", _START, _draft(_climb("6b"))))
    second = await athlete.confirmed(
        await athlete.draft(
            "and a 6b+",
            _START + timedelta(minutes=4),
            _draft(_climb("6b+")),
            extends=first.session_id,
        )
    )
    third = await athlete.confirmed(
        await athlete.draft(
            "then 6c", _START + timedelta(minutes=9), _draft(_climb("6c")), extends=first.session_id
        )
    )

    assert first.session_id == second.session_id == third.session_id
    assert await _session_count(db_session_factory, athlete.user_id) == 1
    assert await _sources(db_session_factory, first.session_id) == [
        "sent 6b",
        "and a 6b+",
        "then 6c",
    ]
    stored = await _session(db_session_factory, first.session_id)
    assert _grades(stored.payload) == ["6b", "6b+", "6c"]
    assert stored.updated_at == _START + timedelta(minutes=9)


async def test_an_extending_draft_is_shown_with_the_session_it_extends(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "extend-shown")
    first = await athlete.confirmed(await athlete.draft("sent 6b", _START, _draft(_climb("6b"))))
    later = _START + timedelta(minutes=4)

    added = await athlete.draft("and a 6b+", later, _draft(_climb("6b+")), extends=first.session_id)

    assert first.session_id is not None
    span = SessionSpan(
        id=first.session_id, occurred_on=_DAY, first_logged_at=_START, last_logged_at=_START
    )
    assert added.extends == span
    shown = await athlete.drafts.list_shown(athlete.user_id, athlete.conversation_id)
    assert [draft.extends for draft in shown] == [None, span]
    confirmed = await athlete.confirmed(added)
    assert confirmed.extends == span.model_copy(update={"last_logged_at": later})
    assert await athlete.drafts.get(athlete.user_id, added.id) == confirmed


async def test_an_extension_fills_only_the_envelope_fields_the_session_lacks(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "extend-envelope")
    first = await athlete.confirmed(
        await athlete.draft("sent 6b, rpe 6", _START, _draft(_climb("6b"), rpe=6))
    )

    await athlete.confirmed(
        await athlete.draft(
            "6b+ too, 90 min, felt strong",
            _START + timedelta(minutes=4),
            _draft(_climb("6b+"), rpe=8, duration_min=90, feeling="strong"),
            extends=first.session_id,
        )
    )

    stored = await _session(db_session_factory, first.session_id)
    assert (stored.rpe, stored.duration_min, stored.feeling) == (6, 90, "strong")


async def test_an_extension_naming_another_day_by_its_confirm_saves_a_new_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "extend-edited-day")
    first = await athlete.confirmed(await athlete.draft("sent 6b", _START, _draft(_climb("6b"))))
    yesterday = _draft(_climb("6a"), occurred_on=date(2026, 10, 1), date_source="stated")

    added = await athlete.confirmed(
        await athlete.draft(
            "6a yesterday", _START + timedelta(minutes=4), yesterday, extends=first.session_id
        )
    )

    assert added.session_id not in (None, first.session_id)
    assert added.extends is None
    assert await _sources(db_session_factory, first.session_id) == ["sent 6b"]
    assert await _sources(db_session_factory, added.session_id) == ["6a yesterday"]


async def test_an_extension_of_an_undone_session_saves_a_new_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "extend-undone")
    first = await athlete.confirmed(await athlete.draft("sent 6b", _START, _draft(_climb("6b"))))
    pending = await athlete.draft(
        "and a 6b+", _START + timedelta(minutes=4), _draft(_climb("6b+")), extends=first.session_id
    )
    await athlete.drafts.undo(athlete.user_id, first.id, undone_at=_LATER)

    added = await athlete.confirmed(pending)

    assert added.session_id is not None
    assert added.extends is None
    assert await _sources(db_session_factory, added.session_id) == ["and a 6b+"]


async def test_another_users_session_is_never_extended(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """The draft's key to the session it extends includes its own `user_id` (ADR-0022), so it
    can't point at someone else's session."""
    owner = await _athlete(db_session_factory, "extend-owner")
    stranger = await _athlete(db_session_factory, "extend-stranger")
    theirs = await owner.confirmed(await owner.draft("sent 6b", _START, _draft(_climb("6b"))))

    with pytest.raises(Exception, match="foreign key"):
        await stranger.draft("and a 6b+", _START, _draft(_climb("6b+")), extends=theirs.session_id)

    assert await _sources(db_session_factory, theirs.session_id) == ["sent 6b"]


async def test_an_extension_of_another_sports_session_saves_a_new_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    """The reply only offers a session in the draft's own sport; the confirm checks it too."""
    athlete = await _athlete(db_session_factory, "extend-sport")
    climb = await athlete.confirmed(await athlete.draft("sent 6b", _START, _draft(_climb("6b"))))

    lifted = await athlete.confirmed(
        await athlete.draft(
            "deadlifts",
            _START + timedelta(minutes=4),
            _draft(_GYM),
            sport_id="gym",
            extends=climb.session_id,
        )
    )

    assert lifted.session_id not in (None, climb.session_id)
    assert lifted.extends is None
    assert _grades((await _session(db_session_factory, climb.session_id)).payload) == ["6b"]


# --- undoing one log -----------------------------------------------------------------------


async def test_undoing_an_added_log_removes_only_its_activities_and_source(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "undo-added")
    first = await athlete.confirmed(await athlete.draft("sent 6b", _START, _draft(_climb("6b"))))
    added = await athlete.confirmed(
        await athlete.draft(
            "and a 6b+",
            _START + timedelta(minutes=4),
            _draft(_climb("6b+")),
            extends=first.session_id,
        )
    )

    undone = await athlete.drafts.undo(athlete.user_id, added.id, undone_at=_LATER)

    assert (undone.id, undone.status, undone.session_id) == (added.id, "confirmed", None)
    assert await _sources(db_session_factory, first.session_id) == ["sent 6b"]
    assert _grades((await _session(db_session_factory, first.session_id)).payload) == ["6b"]
    kept = await athlete.drafts.get(athlete.user_id, first.id)
    assert kept.session_id == first.session_id


async def test_undoing_the_first_log_keeps_the_logs_added_after_it(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "undo-first")
    first = await athlete.confirmed(await athlete.draft("sent 6b", _START, _draft(_climb("6b"))))
    added = await athlete.confirmed(
        await athlete.draft(
            "and a 6b+",
            _START + timedelta(minutes=4),
            _draft(_climb("6b+")),
            extends=first.session_id,
        )
    )

    await athlete.drafts.undo(athlete.user_id, first.id, undone_at=_LATER)

    assert await _sources(db_session_factory, added.session_id) == ["and a 6b+"]
    assert _grades((await _session(db_session_factory, added.session_id)).payload) == ["6b+"]
    assert (await athlete.drafts.get(athlete.user_id, first.id)).session_id is None


async def test_undoing_every_log_removes_the_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "undo-all")
    first = await athlete.confirmed(await athlete.draft("sent 6b", _START, _draft(_climb("6b"))))
    added = await athlete.confirmed(
        await athlete.draft(
            "and a 6b+",
            _START + timedelta(minutes=4),
            _draft(_climb("6b+")),
            extends=first.session_id,
        )
    )

    await athlete.drafts.undo(athlete.user_id, added.id, undone_at=_LATER)
    await athlete.drafts.undo(athlete.user_id, first.id, undone_at=_LATER)

    assert await _session_count(db_session_factory, athlete.user_id) == 0
    with pytest.raises(SessionNotFoundError):
        await athlete.drafts.undo(athlete.user_id, added.id, undone_at=_LATER)


async def test_undoing_another_users_log_is_not_found_and_changes_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    owner = await _athlete(db_session_factory, "undo-log-owner")
    stranger = await _athlete(db_session_factory, "undo-log-stranger")
    theirs = await owner.confirmed(await owner.draft("sent 6b", _START, _draft(_climb("6b"))))

    with pytest.raises(DraftNotFoundError):
        await stranger.drafts.undo(stranger.user_id, theirs.id, undone_at=_LATER)
    with pytest.raises(DraftNotFoundError):
        await stranger.drafts.undo(stranger.user_id, uuid4(), undone_at=_LATER)

    assert await _sources(db_session_factory, theirs.session_id) == ["sent 6b"]


async def test_undoing_a_pending_draft_finds_no_session(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, "undo-pending")
    pending = await athlete.draft("sent 6b", _START, _draft(_climb("6b")))

    with pytest.raises(SessionNotFoundError):
        await athlete.drafts.undo(athlete.user_id, pending.id, undone_at=_LATER)
