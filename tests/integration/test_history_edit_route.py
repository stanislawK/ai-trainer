"""`GET` and `POST /history/{id}/edit` on PostgreSQL (PRD 0011 B4, B38, ADR-0032, ADR-0022,
ticket #516): the athlete's edit of a saved gym session replaces its sets and keeps each entry's
source and exercise, a rejected value is marked and saves nothing, the name is shown and never
posted, a new band joins the athlete's bands unranked and asks nothing, and another athlete's
session is a 404 that changes nothing."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_history import SqlAlchemyExerciseHistory
from ai_trainer.adapters.gym_session_reader import SqlAlchemyGymSessionReader
from ai_trainer.adapters.training_session_editor import SqlAlchemySessionEditor
from ai_trainer.application.session_history import GymSessionEdit, GymSessionHistory
from ai_trainer.domain.users import User
from ai_trainer.web.history import build_history_router
from ai_trainer.web.templating import build_templates
from tests.activity_support import STORES
from tests.chat_support import FROZEN_NOW, FixedClock, sign_in_as
from tests.integration.test_history_route import _SET, _session, _user

_DAY = date(2026, 11, 5)
_AT = datetime(2026, 11, 5, 17, 40, tzinfo=UTC)
_BENCH: list[_SET] = [(10, Decimal("60"), "60 kg"), (8, Decimal("70"), "70 kg")]
_ROWS: list[_SET] = [(12, Decimal("30"), "30 kg")] * 3


def _client(factory: Callable[[], AsyncSession], user: User) -> TestClient:
    reader = SqlAlchemyGymSessionReader(factory)
    app = FastAPI()
    app.include_router(
        build_history_router(
            build_templates(),
            history=GymSessionHistory(reader),
            editing=GymSessionEdit(
                reader, SqlAlchemySessionEditor(factory, stores=STORES, clock=FixedClock())
            ),
        )
    )
    sign_in_as(app, user.model_copy(update={"timezone": "Europe/Warsaw"}))
    return TestClient(app)


def _set_fields(exercise: int, position: int, src: int | None, **values: str) -> dict[str, str]:
    """Every field of one set row, as the form posts them: the ones not given blank."""
    prefix = f"activities.{exercise}.sets.{position}."
    row = {f"{prefix}{field}": "" for field in ("reps", "load", "duration", "distance", "band")}
    row |= {f"{prefix}{field}": value for field, value in values.items()}
    row[f"{prefix}src"] = "" if src is None else str(src)
    return row


async def _rows(factory: Callable[[], AsyncSession], session_id: UUID) -> list[Any]:
    async with factory() as db:
        rows = await db.execute(
            text(
                "SELECT e.id, e.source_id, e.exercise_id, e.name_as_typed, e.measure, s.set_no,"
                " s.reps, s.load_kg, s.load_original, s.band_id, s.band_label"
                " FROM gym_exercise_entries e LEFT JOIN gym_sets s ON s.entry_id = e.id"
                " WHERE e.session_id = :id ORDER BY e.position, s.set_no"
            ),
            {"id": session_id},
        )
        return [tuple(row) for row in rows.all()]


async def _stored_session(factory: Callable[[], AsyncSession], session_id: UUID) -> Any:
    async with factory() as db:
        return (
            await db.execute(
                text("SELECT payload, updated_at FROM training_sessions WHERE id = :id"),
                {"id": session_id},
            )
        ).one()


async def _exercise_id(factory: Callable[[], AsyncSession], session_id: UUID) -> UUID:
    async with factory() as db:
        found = await db.scalar(
            text(
                "SELECT exercise_id FROM gym_exercise_entries"
                " WHERE session_id = :id AND position = 1"
            ),
            {"id": session_id},
        )
    assert isinstance(found, UUID)
    return found


# --- AC: 60 kg to 65 kg shows in history and in exercise_history ---------------------------


async def test_changed_load_shows_in_history_and_exercise_history(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    session_id = await _session(
        db_session_factory, user, _DAY, _AT, {"bench": _BENCH},
        canonical={"bench": "Barbell Bench Press"},
    )  # fmt: skip
    client = _client(db_session_factory, user)

    response = client.post(
        f"/history/{session_id}/edit",
        data=_set_fields(0, 0, 0, reps="10", load="65") | _set_fields(0, 1, 1, reps="8", load="70"),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert response.headers["HX-Push-Url"] == f"/history/{session_id}"
    assert "10 @ 65 kg · 8 @ 70 kg" in response.text
    assert "10 @ 65 kg · 8 @ 70 kg" in client.get(f"/history/{session_id}").text
    exercise_id = await _exercise_id(db_session_factory, session_id)
    (entry,) = await SqlAlchemyExerciseHistory(db_session_factory).entries(
        user.id, exercise_id, limit=5
    )
    assert [(row.reps, row.load_kg) for row in entry.sets] == [(10, 65.0), (8, 70.0)]
    payload, updated_at = await _stored_session(db_session_factory, session_id)
    assert payload == {}
    assert updated_at == FROZEN_NOW


# --- AC: adding and removing a set persists after a reload ---------------------------------


async def test_added_and_removed_sets_persist_after_reload(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    session_id = await _session(
        db_session_factory, user, _DAY, _AT, {"bench": _BENCH, "rows": _ROWS}
    )
    before = await _rows(db_session_factory, session_id)

    response = _client(db_session_factory, user).post(
        f"/history/{session_id}/edit",
        data=_set_fields(0, 0, 0, reps="10", load="60")
        | _set_fields(0, 1, 1, reps="8", load="70")
        | _set_fields(0, 2, None, reps="6", load="80")
        | _set_fields(1, 0, 0, reps="12", load="30")
        | _set_fields(1, 1, 2, reps="12", load="30"),
    )

    assert response.status_code == 200
    page = _client(db_session_factory, user).get(f"/history/{session_id}").text
    assert "10 @ 60 kg · 8 @ 70 kg · 6 @ 80 kg" in page
    assert "2 × 12 · 30 kg" in page  # noqa: RUF001 -- the History line's own sign
    after = await _rows(db_session_factory, session_id)
    assert [(row[3], row[5], row[6], row[7]) for row in after] == [
        ("bench", 1, 10, Decimal("60.00")),
        ("bench", 2, 8, Decimal("70.00")),
        ("bench", 3, 6, Decimal("80.00")),
        ("rows", 1, 12, Decimal("30.00")),
        ("rows", 2, 12, Decimal("30.00")),
    ]
    # Each entry keeps its row, its source and its name.
    assert {row[:4] for row in after} == {row[:4] for row in before}


# --- AC: a rejected value is marked on its field and nothing is saved ----------------------


async def test_rejected_value_is_marked_and_nothing_saved(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    session_id = await _session(db_session_factory, user, _DAY, _AT, {"bench": _BENCH})
    rows, stored = (
        await _rows(db_session_factory, session_id),
        await _stored_session(db_session_factory, session_id),
    )

    response = _client(db_session_factory, user).post(
        f"/history/{session_id}/edit",
        data=_set_fields(0, 0, 0, reps="0", load="65") | _set_fields(0, 1, 1, reps="8", load="70"),
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert (
        'name="activities.0.sets.0.reps" inputmode="numeric" min="0" class="lg-field'
        ' tabular-nums" value="0" aria-invalid="true"'
    ) in response.text
    assert "Enter reps, 1 or more." in response.text
    assert 'value="65"' in response.text
    assert await _rows(db_session_factory, session_id) == rows
    assert await _stored_session(db_session_factory, session_id) == stored


# --- AC: another user's session id returns 404 and changes nothing -------------------------


async def test_other_users_session_is_404_and_unchanged(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    alice, bob = await _user(db_session_factory), await _user(db_session_factory)
    theirs = await _session(db_session_factory, alice, _DAY, _AT, {"secret lift": _BENCH})
    rows, stored = (
        await _rows(db_session_factory, theirs),
        await _stored_session(db_session_factory, theirs),
    )
    client = _client(db_session_factory, bob)

    shown = client.get(f"/history/{theirs}/edit")
    saved = client.post(
        f"/history/{theirs}/edit",
        data=_set_fields(0, 0, 0, reps="1", load="1") | _set_fields(0, 1, 1, reps="1", load="1"),
    )

    assert shown.status_code == 404
    assert "Secret lift" not in shown.text
    assert saved.status_code == 404
    assert await _rows(db_session_factory, theirs) == rows
    assert await _stored_session(db_session_factory, theirs) == stored


async def test_unknown_session_is_404(db_session_factory: Callable[[], AsyncSession]) -> None:
    user = await _user(db_session_factory)
    client = _client(db_session_factory, user)

    assert client.get("/history/not-an-id/edit").status_code == 404
    assert client.post("/history/not-an-id/edit", data={}).status_code == 404


async def test_session_whose_rows_no_longer_read_is_409(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    session_id = await _session(db_session_factory, user, _DAY, _AT, {"bench": []})
    before = await _rows(db_session_factory, session_id)

    response = _client(db_session_factory, user).post(
        f"/history/{session_id}/edit", data=_set_fields(0, 0, None, reps="8", load="60")
    )

    assert response.status_code == 409
    assert await _rows(db_session_factory, session_id) == before


# --- The form -------------------------------------------------------------------------------


async def test_edit_form_shows_name_read_only(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    session_id = await _session(
        db_session_factory, user, _DAY, _AT, {"bench": _BENCH},
        canonical={"bench": "Barbell Bench Press"},
    )  # fmt: skip

    page = _client(db_session_factory, user).get(f"/history/{session_id}/edit").text

    assert 'aria-label="Edit session"' in page
    assert "Edit Thu 5 Nov" in page
    assert "Barbell Bench Press" in page
    assert 'name="activities.0.name"' not in page
    assert 'name="activities.0.sets.1.load" ' in page
    assert f'hx-post="/history/{session_id}/edit"' in page
    assert 'type="date"' not in page
    assert "/static/js/gym_sets.js" in page


async def test_edit_htmx_request_gets_partial(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    session_id = await _session(db_session_factory, user, _DAY, _AT, {"bench": _BENCH})

    partial = (
        _client(db_session_factory, user)
        .get(f"/history/{session_id}/edit", headers={"HX-Request": "true"})
        .text
    )

    assert '<div id="history-content"' in partial
    assert "<html" not in partial
    assert 'aria-label="Edit session"' in partial


async def test_posted_name_is_ignored(db_session_factory: Callable[[], AsyncSession]) -> None:
    user = await _user(db_session_factory)
    session_id = await _session(
        db_session_factory, user, _DAY, _AT, {"bench": _BENCH},
        canonical={"bench": "Barbell Bench Press"},
    )  # fmt: skip
    before = await _rows(db_session_factory, session_id)

    response = _client(db_session_factory, user).post(
        f"/history/{session_id}/edit",
        data={"activities.0.name": "squat"}
        | _set_fields(0, 0, 0, reps="10", load="60")
        | _set_fields(0, 1, 1, reps="8", load="70"),
    )

    assert response.status_code == 200
    assert [row[:4] for row in await _rows(db_session_factory, session_id)] == [
        row[:4] for row in before
    ]


async def test_cancel_returns_session_view(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    session_id = await _session(db_session_factory, user, _DAY, _AT, {"bench": _BENCH})
    client = _client(db_session_factory, user)

    form = client.get(f"/history/{session_id}/edit").text
    view = client.get(f"/history/{session_id}").text

    assert f'hx-get="/history/{session_id}"' in form
    assert 'aria-label="Saved session"' in view
    assert f'hx-get="/history/{session_id}/edit"' in view


# --- Bands (owner's answer at the plan gate: a new band joins unranked) ---------------------


async def _bands(factory: Callable[[], AsyncSession], user_id: UUID) -> list[Any]:
    async with factory() as db:
        rows = await db.execute(
            text("SELECT id, label, rank FROM athlete_bands WHERE user_id = :id ORDER BY label"),
            {"id": user_id},
        )
        return [tuple(row) for row in rows.all()]


async def _clarification_count(factory: Callable[[], AsyncSession], user_id: UUID) -> int:
    async with factory() as db:
        return (
            await db.scalar(
                text("SELECT count(*) FROM clarifications WHERE user_id = :id"), {"id": user_id}
            )
            or 0
        )


async def test_new_band_is_saved_unranked_and_asks_nothing(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    session_id = await _session(db_session_factory, user, _DAY, _AT, {"rows": _ROWS[:1]})

    response = _client(db_session_factory, user).post(
        f"/history/{session_id}/edit",
        data={"activities.0.measure": "band_reps"} | _set_fields(0, 0, 0, reps="12", band="blue"),
    )

    assert response.status_code == 200
    ((band_id, label, rank),) = await _bands(db_session_factory, user.id)
    assert (label, rank) == ("blue", None)
    ((*_, measure, _no, reps, load_kg, _original, set_band, set_label),) = await _rows(
        db_session_factory, session_id
    )
    assert (measure, reps, load_kg, set_band, set_label) == (
        "band_reps",
        12,
        None,
        band_id,
        "blue",
    )
    assert await _clarification_count(db_session_factory, user.id) == 0


async def test_known_band_any_case_keeps_its_band_id(
    db_session_factory: Callable[[], AsyncSession],
) -> None:
    user = await _user(db_session_factory)
    session_id = await _session(db_session_factory, user, _DAY, _AT, {"rows": _ROWS[:1]})
    async with db_session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO athlete_bands (id, user_id, label, rank)"
                " VALUES (gen_random_uuid(), :id, 'Red', 1)"
            ),
            {"id": user.id},
        )
        await db.commit()

    _client(db_session_factory, user).post(
        f"/history/{session_id}/edit",
        data=_set_fields(0, 0, 0, reps="12", band=" red "),
    )

    ((band_id, label, rank),) = await _bands(db_session_factory, user.id)
    assert (label, rank) == ("Red", 1)
    assert (await _rows(db_session_factory, session_id))[0][-2:] == (band_id, "red")
