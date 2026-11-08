"""`GET /history/{session_id}/exercises/{index}` on PostgreSQL (PRD 0011 M2g, B37, B40, ADR-0031,
ticket #517): one exercise's loads in date order, the entries saved under a typed name before it
was settled, the empty state with no numbers, and another athlete's history never shown."""

import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from html import unescape
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_history import SqlAlchemyExerciseHistory
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.gym_band_order import SqlAlchemyBandOrder
from ai_trainer.adapters.gym_session_reader import SqlAlchemyGymSessionReader
from ai_trainer.adapters.orm import (
    AthleteBandOrm,
    ExerciseOrm,
    GymExerciseEntryOrm,
    GymSetOrm,
    TrainingSessionOrm,
    TrainingSessionSourceOrm,
)
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.exercise_progress import ExerciseProgress
from ai_trainer.application.session_history import GymSessionHistory
from ai_trainer.application.training_history import ExerciseTrainingHistory
from ai_trainer.domain.exercises import ExerciseMeasure
from ai_trainer.domain.users import NewUser, User, UserStatus
from ai_trainer.web.exercise_progress import build_exercise_progress_router
from ai_trainer.web.templating import build_templates
from tests.chat_support import sign_in_as
from tests.integration.test_gym_activity_store import (
    _LATER,
    _custom,
    _gym,
    _lift,
    _logged,
    _settled,
)
from tests.integration.test_session_continuity_repository import _athlete

_SET = tuple[int | None, Decimal | None, str | None]


async def _user(factory: Callable[[], AsyncSession]) -> User:
    sub = f"progress-{uuid4()}"
    return await SqlAlchemyUsersRepository(factory).create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=None, locale="en", status=UserStatus.ACTIVE
        )
    )


async def _exercise(factory: Callable[[], AsyncSession], user: User, name: str) -> UUID:
    exercise_id = uuid4()
    async with factory() as db:
        db.add(
            ExerciseOrm(
                id=exercise_id,
                sport_id="gym",
                owner_user_id=user.id,
                source="custom",
                name=name,
                default_measure="load_reps",
            )
        )
        await db.commit()
    return exercise_id


async def _log(
    factory: Callable[[], AsyncSession],
    user: User,
    day: date,
    name: str,
    sets: list[_SET],
    *,
    exercise_id: UUID | None = None,
    measure: str = "load_reps",
    band: str | None = None,
) -> UUID:
    """A one-entry gym session on `day`; `sets` are (reps, kg, text as typed)."""
    session_id, source_id, entry_id = uuid4(), uuid4(), uuid4()
    logged_at = datetime(day.year, day.month, day.day, 17, 0, tzinfo=UTC)
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
        db.add(
            GymExerciseEntryOrm(
                id=entry_id,
                user_id=user.id,
                session_id=session_id,
                source_id=source_id,
                position=1,
                exercise_id=exercise_id,
                name_as_typed=name,
                measure=measure,
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
                    band_label=band,
                )
            )
        await db.commit()
    return session_id


def _client(factory: Callable[[], AsyncSession], user: User) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_exercise_progress_router(
            build_templates(),
            progress=ExerciseProgress(
                GymSessionHistory(SqlAlchemyGymSessionReader(factory)),
                ExerciseTrainingHistory(
                    SqlAlchemyExerciseRepository(factory), SqlAlchemyExerciseHistory(factory)
                ),
                SqlAlchemyBandOrder(factory),
            ),
        )
    )
    sign_in_as(app, user.model_copy(update={"timezone": "Europe/Warsaw"}))
    return TestClient(app)


def _content_text(page: str) -> str:
    """The visible text of the page's own content, below its title: no markup, no icons."""
    content = page.split('id="history-content"', 1)[1].split("</h1>", 1)[1]
    return re.sub(r"<[^>]+>", " ", re.sub(r"<svg.*?</svg>", "", content, flags=re.S))


def _chart(page: str) -> dict[str, object]:
    raw = page.split('data-chart="', 1)[1].split('"', 1)[0]
    chart: dict[str, object] = json.loads(unescape(raw))
    return chart


async def test_progress_shows_each_load_in_date_order(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    raise_id = await _exercise(db_session_factory, user, "Lateral Raise")
    await _log(
        db_session_factory, user, date(2026, 10, 27), "lateral raise",
        [(12, Decimal("6"), "6 kg")] * 3, exercise_id=raise_id,
    )  # fmt: skip
    latest = await _log(
        db_session_factory, user, date(2026, 11, 3), "lateral raise",
        [(12, Decimal("7"), "7 kg")] * 3, exercise_id=raise_id,
    )  # fmt: skip

    page = _client(db_session_factory, user).get(f"/history/{latest}/exercises/0").text

    chart = _chart(page)
    assert chart["labels"] == ["27 Oct", "3 Nov"]
    assert chart["values"] == [6.0, 7.0]
    assert chart["unit"] == "kg"
    assert page.index("12 @ 7 kg") < page.index("12 @ 6 kg")
    assert "Lateral Raise" in page
    assert f'href="/history/{latest}"' in page
    assert "Tue 3 Nov" in page
    assert 'role="table"' in page


async def test_progress_includes_typed_entries_linked_at_settle(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    athlete = await _athlete(db_session_factory, f"progress-settle-{uuid4()}")
    user = await SqlAlchemyUsersRepository(db_session_factory).get(athlete.user_id)
    assert user is not None
    exercise_id = await _custom(athlete, db_session_factory, "Bench")
    await _logged(athlete, "old", _gym(_lift("bench press", 3, 8, "50kg", 50)))
    settled = await _logged(
        athlete, "new", _settled(exercise_id, "bench press"), at=_LATER + timedelta(days=1)
    )
    client = _client(db_session_factory, user)
    sessions = await GymSessionHistory(SqlAlchemyGymSessionReader(db_session_factory)).list(
        user.id, "UTC"
    )
    opened = next(s for s in sessions if s.id == settled.session_id)
    index = next(i for i, e in enumerate(opened.exercises) if e.exercise_id == exercise_id)

    page = client.get(f"/history/{opened.id}/exercises/{index}").text

    assert _chart(page)["values"] == [50.0, 60.0]


async def test_typed_only_entry_opens_its_typed_history(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    await _log(
        db_session_factory, user, date(2026, 10, 27), "Cable  Fly", [(10, Decimal("20"), "20 kg")]
    )
    latest = await _log(
        db_session_factory, user, date(2026, 11, 3), "cable fly", [(10, Decimal("25"), "25 kg")]
    )

    page = _client(db_session_factory, user).get(f"/history/{latest}/exercises/0").text

    assert _chart(page)["values"] == [20.0, 25.0]
    assert "Cable fly" in page


async def test_progress_without_sets_shows_empty_state(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    session = await _log(
        db_session_factory, user, date(2026, 11, 3), "face pull", [(None, None, None)] * 3
    )

    page = _client(db_session_factory, user).get(f"/history/{session}/exercises/0").text

    assert "No sessions with this exercise yet" in page
    assert "<canvas" not in page
    assert 'role="table"' not in page
    assert not any(ch.isdigit() for ch in _content_text(page))


async def test_other_users_session_progress_is_404(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    alice, bob = await _user(db_session_factory), await _user(db_session_factory)
    theirs = await _log(
        db_session_factory, bob, date(2026, 11, 3), "secret lift", [(5, Decimal("100"), "100 kg")]
    )

    response = _client(db_session_factory, alice).get(f"/history/{theirs}/exercises/0")

    assert response.status_code == 404
    assert "Secret lift" not in response.text


async def test_progress_counts_only_own_entries(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    alice, bob = await _user(db_session_factory), await _user(db_session_factory)
    shared = uuid4()
    async with db_session_factory() as db:
        db.add(
            ExerciseOrm(
                id=shared,
                sport_id="gym",
                owner_user_id=None,
                source="test",
                source_ref=str(shared),
                name="Shared Press",
                default_measure="load_reps",
            )
        )
        await db.commit()
    mine = await _log(
        db_session_factory, alice, date(2026, 11, 3), "press", [(5, Decimal("40"), "40 kg")],
        exercise_id=shared,
    )  # fmt: skip
    await _log(
        db_session_factory, bob, date(2026, 11, 2), "press", [(5, Decimal("99"), "99 kg")],
        exercise_id=shared,
    )  # fmt: skip

    page = _client(db_session_factory, alice).get(f"/history/{mine}/exercises/0").text

    assert _chart(page)["values"] == [40.0]
    assert "99" not in _content_text(page)


async def test_unknown_session_or_index_is_404(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    session = await _log(db_session_factory, user, date(2026, 11, 3), "row", [(5, None, None)])
    client = _client(db_session_factory, user)

    assert client.get(f"/history/{uuid4()}/exercises/0").status_code == 404
    assert client.get(f"/history/{session}/exercises/1").status_code == 404
    assert client.get("/history/not-an-id/exercises/0").status_code == 404


async def test_band_progress_plots_places_and_marks_the_unranked(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    async with db_session_factory() as db:
        for rank, label in enumerate(("red", "black"), start=1):
            db.add(AthleteBandOrm(id=uuid4(), user_id=user.id, label=label, rank=rank))
        await db.commit()
    for day, band in ((date(2026, 10, 8), "red"), (date(2026, 11, 3), "blue")):
        await _log(
            db_session_factory, user, day, "band row", [(10, None, None)],
            measure="band_reps", band=band,
        )  # fmt: skip
    latest = (
        await GymSessionHistory(SqlAlchemyGymSessionReader(db_session_factory)).list(user.id, "UTC")
    )[0].id

    page = _client(db_session_factory, user).get(f"/history/{latest}/exercises/0").text

    chart = _chart(page)
    assert chart["values"] == [1.0, None]
    assert chart["order"] == ["red", "black"]
    assert "blue · not in your order" in page
    assert ExerciseMeasure.BAND_REPS.value == "band_reps"


async def test_progress_labels_carry_the_year_when_entries_span_years(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    raise_id = await _exercise(db_session_factory, user, "Lateral Raise")
    await _log(
        db_session_factory, user, date(2025, 11, 3), "lateral raise",
        [(12, Decimal("6"), "6 kg")], exercise_id=raise_id,
    )  # fmt: skip
    latest = await _log(
        db_session_factory, user, date(2026, 11, 3), "lateral raise",
        [(12, Decimal("7"), "7 kg")], exercise_id=raise_id,
    )  # fmt: skip

    page = _client(db_session_factory, user).get(f"/history/{latest}/exercises/0").text

    assert _chart(page)["labels"] == ["3 Nov 2025", "3 Nov 2026"]
    assert "3 Nov 2025" in page.split('role="table"', 1)[1]


async def test_progress_with_reps_and_no_kg_lists_rows_without_a_chart(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    session = await _log(
        db_session_factory, user, date(2026, 11, 3), "pull up", [(8, None, None)] * 2
    )

    page = _client(db_session_factory, user).get(f"/history/{session}/exercises/0").text

    assert "No sessions with this exercise yet" not in page
    assert "8 reps" in page
    assert "<canvas" not in page
