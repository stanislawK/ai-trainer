"""`GET /history` and `GET /history/{id}` on PostgreSQL (PRD 0011 F3, B4, ADR-0005, ticket #515):
the athlete's own gym sessions newest first with canonical names and sets, the empty state, the
session view, and another athlete's sessions never listed and never opened."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.gym_session_reader import SqlAlchemyGymSessionReader
from ai_trainer.adapters.orm import (
    ExerciseOrm,
    GymExerciseEntryOrm,
    GymSetOrm,
    TrainingSessionOrm,
    TrainingSessionSourceOrm,
)
from ai_trainer.adapters.training_session_editor import SqlAlchemySessionEditor
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.session_history import GymSessionEdit, GymSessionHistory
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.history import build_history_router
from ai_trainer.web.templating import build_templates
from tests.activity_support import STORES
from tests.chat_support import FixedClock, sign_in_as

_SET = tuple[int | None, Decimal | None, str | None]


async def _user(factory: Callable[[], AsyncSession]) -> User:
    sub = f"history-{uuid4()}"
    return await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )


async def _session(
    factory: Callable[[], AsyncSession],
    user: User,
    day: date,
    logged_at: datetime,
    exercises: dict[str, list[_SET]],
    *,
    canonical: dict[str, str] | None = None,
) -> UUID:
    """A gym session with its source log and one entry per `exercises` item, its sets as
    (reps, kg, text as typed); `canonical` settles a typed name to a custom exercise."""
    session_id, source_id = uuid4(), uuid4()
    async with factory() as db:
        db.add(
            TrainingSessionOrm(
                id=session_id,
                user_id=user.id,
                sport_id="gym",
                occurred_on=day,
                timezone="Europe/Warsaw",
                payload={},
                payload_version=2,
                created_at=logged_at,
                updated_at=logged_at,
            )
        )
        await db.flush()
        db.add(
            TrainingSessionSourceOrm(
                id=source_id,
                user_id=user.id,
                session_id=session_id,
                raw_text="log",
                source="chat",
                logged_at=logged_at,
            )
        )
        await db.flush()
        for position, (name, sets) in enumerate(exercises.items(), start=1):
            exercise_id = None
            if canonical and name in canonical:
                exercise_id = uuid4()
                db.add(
                    ExerciseOrm(
                        id=exercise_id,
                        sport_id="gym",
                        owner_user_id=user.id,
                        source="custom",
                        name=canonical[name],
                        default_measure="load_reps",
                    )
                )
                await db.flush()
            entry_id = uuid4()
            db.add(
                GymExerciseEntryOrm(
                    id=entry_id,
                    user_id=user.id,
                    session_id=session_id,
                    source_id=source_id,
                    position=position,
                    exercise_id=exercise_id,
                    name_as_typed=name,
                    measure="load_reps",
                )
            )
            await db.flush()
            for set_no, (reps, kg, original) in enumerate(sets, start=1):
                db.add(
                    GymSetOrm(
                        user_id=user.id,
                        entry_id=entry_id,
                        set_no=set_no,
                        reps=reps,
                        load_kg=kg,
                        load_original=original,
                    )
                )
        await db.commit()
    return session_id


def _client(factory: Callable[[], AsyncSession], user: User) -> TestClient:
    app = FastAPI()
    reader = SqlAlchemyGymSessionReader(factory)
    editor = SqlAlchemySessionEditor(factory, stores=STORES, clock=FixedClock())
    app.include_router(
        build_history_router(
            build_templates(),
            history=GymSessionHistory(reader),
            editing=GymSessionEdit(reader, editor),
        )
    )
    sign_in_as(app, user.model_copy(update={"timezone": "Europe/Warsaw"}))
    return TestClient(app)


_BENCH: list[_SET] = [(8, Decimal("60"), "60 kg")] * 3


async def test_lists_three_gym_sessions_newest_first_with_canonical_names_and_sets(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    await _session(
        db_session_factory, user, date(2026, 10, 1), datetime(2026, 10, 1, 16, 30, tzinfo=UTC),
        {"squat": [(5, Decimal("100"), "100 kg")]},
    )  # fmt: skip
    await _session(
        db_session_factory, user, date(2026, 11, 3), datetime(2026, 11, 3, 17, 40, tzinfo=UTC),
        {"bench": _BENCH}, canonical={"bench": "Barbell Bench Press"},
    )  # fmt: skip
    await _session(
        db_session_factory, user, date(2026, 10, 20), datetime(2026, 10, 20, 6, 15, tzinfo=UTC),
        {"pull-up": [(8, None, None)] * 3},
    )  # fmt: skip

    page = _client(db_session_factory, user).get("/history").text

    assert page.index("Barbell Bench Press") < page.index("Pull-up") < page.index("Squat")
    assert "3 \u00d7 8 · 60 kg" in page
    assert "3 \u00d7 8" in page
    assert "18:40 · 1 exercise" in page


async def test_empty_history_shows_empty_state(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)

    page = _client(db_session_factory, user).get("/history").text

    assert "No sessions yet" in page
    assert "Log a session in the chat" in page


async def test_other_users_sessions_never_listed(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    alice, bob = await _user(db_session_factory), await _user(db_session_factory)
    await _session(
        db_session_factory, bob, date(2026, 11, 3), datetime(2026, 11, 3, 17, 40, tzinfo=UTC),
        {"secret lift": _BENCH},
    )  # fmt: skip

    page = _client(db_session_factory, alice).get("/history").text

    assert "Secret lift" not in page
    assert "No sessions yet" in page


async def test_other_users_session_id_is_404(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    alice, bob = await _user(db_session_factory), await _user(db_session_factory)
    theirs = await _session(
        db_session_factory, bob, date(2026, 11, 3), datetime(2026, 11, 3, 17, 40, tzinfo=UTC),
        {"secret lift": _BENCH},
    )  # fmt: skip

    response = _client(db_session_factory, alice).get(f"/history/{theirs}")

    assert response.status_code == 404
    assert "Secret lift" not in response.text


async def test_unknown_session_id_is_404(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    client = _client(db_session_factory, user)

    assert client.get(f"/history/{uuid4()}").status_code == 404
    assert client.get("/history/not-an-id").status_code == 404


async def test_session_view_shows_exercise_lines(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    opened = await _session(
        db_session_factory, user, date(2026, 11, 5), datetime(2026, 11, 5, 17, 40, tzinfo=UTC),
        {"bench": _BENCH}, canonical={"bench": "Barbell Bench Press"},
    )  # fmt: skip

    page = _client(db_session_factory, user).get(f"/history/{opened}").text

    assert 'aria-label="Saved session"' in page
    assert "Thu 5 Nov" in page
    assert "18:40 · Gym · 1 exercise" in page
    assert "3 \u00d7 8 · 60 kg" in page
    assert f'hx-get="/history/{opened}/edit"' in page
    assert "Delete" not in page
    assert 'aria-current="true"' in page


async def test_session_rows_link_to_progress(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    opened = await _session(
        db_session_factory, user, date(2026, 11, 5), datetime(2026, 11, 5, 17, 40, tzinfo=UTC),
        {"bench": _BENCH, "squat": _BENCH},
    )  # fmt: skip

    page = _client(db_session_factory, user).get(f"/history/{opened}").text

    assert f'href="/history/{opened}/exercises/0"' in page
    assert f'href="/history/{opened}/exercises/1"' in page


async def test_htmx_request_gets_partial(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    opened = await _session(
        db_session_factory, user, date(2026, 11, 5), datetime(2026, 11, 5, 17, 40, tzinfo=UTC),
        {"bench": _BENCH},
    )  # fmt: skip
    client = _client(db_session_factory, user)

    for path in ("/history", f"/history/{opened}"):
        partial = client.get(path, headers={"HX-Request": "true"}).text

        assert '<div id="history-content"' in partial
        assert "<html" not in partial
