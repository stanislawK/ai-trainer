"""`DELETE /history/{id}` on PostgreSQL (PRD 0011 B4, ADR-0022, ADR-0031, ADR-0032, ticket #518):
the athlete's delete of a saved gym session takes it out of History, out of its exercise's
progress and out of `exercise_history`, keeps their alias, custom exercise and band, answers a
session already deleted with the History list and a notice, and answers another athlete's
session with a 404 that deletes nothing."""

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_history import SqlAlchemyExerciseHistory
from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.gym_band_order import SqlAlchemyBandOrder
from ai_trainer.adapters.gym_session_reader import SqlAlchemyGymSessionReader
from ai_trainer.adapters.training_session_editor import (
    SqlAlchemySessionDeleter,
    SqlAlchemySessionEditor,
)
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.exercise_progress import ExerciseProgress
from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.application.ports.conversation import FixedReply
from ai_trainer.application.session_history import (
    GymSessionDelete,
    GymSessionEdit,
    GymSessionHistory,
)
from ai_trainer.application.training_history import ExerciseTrainingHistory
from ai_trainer.domain.sports.gym.payload import GymPayload
from ai_trainer.domain.users import User
from ai_trainer.web.exercise_progress import build_exercise_progress_router
from ai_trainer.web.history import build_history_router
from ai_trainer.web.templating import build_templates
from tests.activity_support import STORES
from tests.chat_support import FixedClock, sign_in_as
from tests.integration.test_exercise_progress_route import _log
from tests.integration.test_gym_activity_store import _custom, _gym, _id, _lift, _logged
from tests.integration.test_session_continuity_repository import _Athlete, _athlete, _draft
from tests.picker_support import ScriptedPicker

SessionFactory = Callable[[], AsyncSession]
_HTMX = {"HX-Request": "true"}
_NOTICE = "This session was already deleted."


def _client(factory: SessionFactory, user: User) -> TestClient:
    reader = SqlAlchemyGymSessionReader(factory)
    history = GymSessionHistory(reader)
    app = FastAPI()
    templates = build_templates()
    app.include_router(
        build_history_router(
            templates,
            history=history,
            editing=GymSessionEdit(
                reader, SqlAlchemySessionEditor(factory, stores=STORES, clock=FixedClock())
            ),
            deleting=GymSessionDelete(SqlAlchemySessionDeleter(factory)),
        )
    )
    app.include_router(
        build_exercise_progress_router(
            templates,
            progress=ExerciseProgress(
                history,
                ExerciseTrainingHistory(
                    SqlAlchemyExerciseRepository(factory), SqlAlchemyExerciseHistory(factory)
                ),
                SqlAlchemyBandOrder(factory),
            ),
        )
    )
    sign_in_as(app, user.model_copy(update={"timezone": "Europe/Warsaw"}))
    return TestClient(app)


async def _user(factory: SessionFactory, athlete: _Athlete) -> User:
    user = await SqlAlchemyUsersRepository(factory).get(athlete.user_id)
    assert user is not None
    return user


async def _bench_and_band(athlete: _Athlete, exercise_id: UUID) -> UUID:
    """A confirmed log of bench press, settled on the card as `exercise_id` (which stores its
    alias), and band rows with a red band the athlete hadn't used before."""
    payload = GymPayload.model_validate(
        _gym(
            _lift("bench press", 3, 8, "60kg", 60),
            {"name": "band rows", "sets": 3, "reps": 10, "band": "red"},
        ).payload
    )
    payload.activities[0].exercise_id = exercise_id
    confirmed = await _logged(athlete, "bench and rows", _draft(payload.model_dump(mode="json")))
    return _id(confirmed.session_id)


async def _rows(factory: SessionFactory, session_id: UUID) -> tuple[int, ...]:
    """The session's own rows: itself, its sources, its gym entries and its sets."""
    async with factory() as db:
        counts = []
        for sql in (
            "SELECT count(*) FROM training_sessions WHERE id = :id",
            "SELECT count(*) FROM training_session_sources WHERE session_id = :id",
            "SELECT count(*) FROM gym_exercise_entries WHERE session_id = :id",
            "SELECT count(*) FROM gym_sets s JOIN gym_exercise_entries e ON e.id = s.entry_id"
            " WHERE e.session_id = :id",
        ):
            counts.append(int(await db.scalar(text(sql), {"id": session_id}) or 0))
        return tuple(counts)


async def _orphan_sets(factory: SessionFactory, user_id: UUID) -> int:
    async with factory() as db:
        found = await db.scalar(
            text(
                "SELECT count(*) FROM gym_sets s"
                " WHERE s.user_id = :id AND NOT EXISTS"
                " (SELECT 1 FROM gym_exercise_entries e WHERE e.id = s.entry_id)"
            ),
            {"id": user_id},
        )
        return int(found or 0)


async def _bands(factory: SessionFactory, user_id: UUID) -> list[Any]:
    async with factory() as db:
        rows = await db.scalars(
            text("SELECT label FROM athlete_bands WHERE user_id = :id"), {"id": user_id}
        )
        return list(rows)


# --- AC: gone from history, from progress and from exercise_history ------------------------


async def test_delete_removes_session_from_history_progress_and_exercise_history(
    db_session_factory: SessionFactory,
) -> None:
    athlete = await _athlete(db_session_factory, "history-delete")
    user = await _user(db_session_factory, athlete)
    exercise_id = await _custom(athlete, db_session_factory, "Bench")
    deleted = await _bench_and_band(athlete, exercise_id)
    kept = await _log(
        db_session_factory, user, date(2026, 10, 9), "bench press",
        [(8, Decimal("70"), "70 kg")] * 3, exercise_id=exercise_id,
    )  # fmt: skip
    client = _client(db_session_factory, user)
    progress = f"/history/{kept}/exercises/0"
    assert "60 kg" in client.get(progress).text
    assert f'href="/history/{deleted}"' in client.get("/history").text

    response = client.delete(f"/history/{deleted}", headers=_HTMX)

    assert response.status_code == 200
    assert response.headers["HX-Push-Url"] == "/history"
    assert '<div id="history-content"' in response.text
    assert f'href="/history/{deleted}"' not in response.text
    assert 'aria-label="Saved session"' not in response.text
    assert _NOTICE not in response.text
    assert f'href="/history/{deleted}"' not in client.get("/history").text
    assert client.get(f"/history/{deleted}").status_code == 404
    assert client.get(f"/history/{deleted}/exercises/0").status_code == 404
    page = client.get(progress)
    assert page.status_code == 200
    assert "70 kg" in page.text
    assert "60 kg" not in page.text
    entries = await SqlAlchemyExerciseHistory(db_session_factory).entries(
        user.id, exercise_id, limit=10
    )
    assert [entry.session_id for entry in entries] == [kept]
    assert await _rows(db_session_factory, deleted) == (0, 0, 0, 0)
    assert await _orphan_sets(db_session_factory, user.id) == 0


# --- AC: the alias still resolves on the next log with no question -------------------------


def _no_question() -> FixedReply:
    raise AssertionError("history resolved the name, so nothing is asked")


async def test_delete_keeps_alias_and_custom_exercise(
    db_session_factory: SessionFactory,
) -> None:
    athlete = await _athlete(db_session_factory, "history-delete-alias")
    user = await _user(db_session_factory, athlete)
    exercise_id = await _custom(athlete, db_session_factory, "Bench")
    deleted = await _bench_and_band(athlete, exercise_id)
    exercises = SqlAlchemyExerciseRepository(db_session_factory)
    assert [a.alias for a in await exercises.user_aliases(user.id)] == ["bench press"]

    response = _client(db_session_factory, user).delete(f"/history/{deleted}", headers=_HTMX)

    assert response.status_code == 200
    [alias] = await exercises.user_aliases(user.id)
    assert (alias.alias, alias.exercise_id) == ("bench press", exercise_id)
    custom = await exercises.get(user.id, exercise_id)
    assert custom is not None
    assert custom.owner_user_id == user.id
    assert await _bands(db_session_factory, user.id) == ["red"]
    # The next log of the name: history (ADR-0031 step 1) settles it, Jev is never asked.
    picker = ScriptedPicker()
    settled = await ResolveExercises(
        exercises,
        picker,
        question=_no_question,
        limits=ResolutionLimits(near_match=0.75, candidate_floor=0.25, shortlist_size=4),
    ).settle(user.id, _gym(_lift("Bench press", 3, 8, "65kg", 65)).payload)
    [exercise] = GymPayload.model_validate(settled).activities
    assert exercise.exercise_id == exercise_id
    assert not exercise.jev_pick
    assert picker.asked == []


# --- AC: a session already deleted in another tab gets a notice, not an error page ----------


async def test_delete_of_already_deleted_session_shows_notice(
    db_session_factory: SessionFactory,
) -> None:
    athlete = await _athlete(db_session_factory, "history-delete-twice")
    user = await _user(db_session_factory, athlete)
    deleted = await _bench_and_band(athlete, await _custom(athlete, db_session_factory, "Bench"))
    kept = await _log(
        db_session_factory, user, date(2026, 10, 9), "squat", [(5, Decimal("100"), "100 kg")]
    )
    client = _client(db_session_factory, user)
    assert client.delete(f"/history/{deleted}", headers=_HTMX).status_code == 200

    again = client.delete(f"/history/{deleted}", headers=_HTMX)

    assert again.status_code == 404
    assert again.headers["HX-Push-Url"] == "/history"
    assert '<div id="history-content"' in again.text
    assert "<html" not in again.text
    assert _NOTICE in again.text
    assert f'href="/history/{kept}"' in again.text
    full = client.delete(f"/history/{deleted}")
    assert full.status_code == 404
    assert "<html" in full.text
    assert _NOTICE in full.text
    assert f'href="/history/{kept}"' in full.text


# --- AC: another user's session id is a 404 and deletes nothing ----------------------------


async def test_delete_of_another_users_session_is_404_and_deletes_nothing(
    db_session_factory: SessionFactory,
) -> None:
    owner = await _athlete(db_session_factory, "history-delete-owner")
    stranger = await _athlete(db_session_factory, "history-delete-stranger")
    theirs = await _bench_and_band(owner, await _custom(owner, db_session_factory, "Bench"))
    before = await _rows(db_session_factory, theirs)
    assert before == (1, 1, 2, 6)

    response = _client(db_session_factory, await _user(db_session_factory, stranger)).delete(
        f"/history/{theirs}", headers=_HTMX
    )

    assert response.status_code == 404
    assert "Bench" not in response.text
    assert await _rows(db_session_factory, theirs) == before
    owners = _client(db_session_factory, await _user(db_session_factory, owner))
    assert owners.get(f"/history/{theirs}").status_code == 200


async def test_delete_of_a_malformed_id_is_404(db_session_factory: SessionFactory) -> None:
    athlete = await _athlete(db_session_factory, "history-delete-malformed")
    client = _client(db_session_factory, await _user(db_session_factory, athlete))

    assert client.delete("/history/not-an-id", headers=_HTMX).status_code == 404
