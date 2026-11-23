"""The flagged and rejected beginner how-tos under `/admin/exercises?view=howtos`, through the
real middleware stack and PostgreSQL (ADR-0031, #594): listed with their status and original;
an edit shows on the card and survives a source change; a cleared text is rewritten by the next
card; an empty or over-long edit is refused with the row unchanged; admin only, CSRF-checked."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_trainer.adapters.exercise_repository import SqlAlchemyExerciseRepository
from ai_trainer.adapters.exercise_simple_text_repository import (
    SqlAlchemyExerciseSimpleTextRepository,
)
from ai_trainer.adapters.sessions_repository import SqlAlchemySessionsRepository
from ai_trainer.adapters.user_status_changer import SqlAlchemyUserStatusChanger
from ai_trainer.adapters.users_repository import SqlAlchemyUsersRepository
from ai_trainer.application.exercise_simple_howto import (
    CardHowtos,
    HowtoReview,
    WriteSimpleHowto,
)
from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.domain.exercise_simple_texts import (
    HowtoPresent,
    HowtoStored,
    LoadHowto,
    NewSimpleText,
    ShortHowto,
    SimpleHowtoDraft,
    source_sha256,
)
from ai_trainer.domain.exercises import (
    CatalogueExercise,
    ExerciseCategory,
    ExerciseMeasure,
    ExerciseText,
)
from ai_trainer.domain.sessions import NewSession
from ai_trainer.domain.users import NewUser, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.admin import build_admin_router
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.csrf import CSRF_HEADER_NAME, CsrfMiddleware
from ai_trainer.web.templating import build_templates
from tests.admin_tokens_support import memory_admin_tokens
from tests.exercise_media_support import memory_exercise_media_review
from tests.feedback_support import InMemoryFeedbackEvents
from tests.knowledge_support import knowledge_wiring
from tests.simple_howto_support import ScriptedCheck, ScriptedWriter

SessionFactory = Callable[[], AsyncSession]
SECRET = b"integration-test-secret"
NOW = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)
FLAGGED_AT = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
MAX_CHARS = 180
_ORIGINAL = "Stand with a dumbbell in each hand. Raise the arms to the sides to shoulder height."
_SHORT = "Hold a dumbbell in each hand and lift your arms out to the sides."

_CSRF_TOKEN_IN_HTML = re.compile(r'hx-headers:inherited=\'\{"X-CSRF-Token": "([0-9a-f]+)"\}\'')


class Clock:
    def now(self) -> datetime:
        return NOW


@dataclass
class Page:
    client: TestClient
    factory: SessionFactory
    csrf: str

    def headers(self) -> Mapping[str, str]:
        return {CSRF_HEADER_NAME: self.csrf, "HX-Request": "true"}

    def card(self, exercise_id: UUID) -> CardHowtos:
        """The exercise card's how-tos over the same database, with a writer that would write
        `Rewritten.`, to show whether a card writes at all."""
        texts = SqlAlchemyExerciseSimpleTextRepository(self.factory)
        return CardHowtos(
            texts=texts,
            write=WriteSimpleHowto(
                texts=texts,
                writer=ScriptedWriter(_draft("Rewritten after the clear.")),
                check=ScriptedCheck(),
                clock=Clock(),
                max_chars=MAX_CHARS,
            ),
            feedback=FeedbackRecorder(InMemoryFeedbackEvents(), clock=Clock()),
            clock=Clock(),
        )

    async def exercise(self, name: str, instructions: str = _ORIGINAL) -> UUID:
        exercise = CatalogueExercise(
            sport_id="gym",
            source="zz-howto-review",
            source_ref=f"zz-howto-review-{uuid4()}",
            name=name,
            equipment="dumbbell",
            body_part="shoulders",
            target="delts",
            secondary_muscles=(),
            default_measure=ExerciseMeasure.LOAD_REPS,
            category=ExerciseCategory.STRENGTH,
            attribution="test",
            texts=(ExerciseText(lang="en", instructions=instructions, steps=()),),
        )
        await SqlAlchemyExerciseRepository(self.factory).upsert_catalogue([exercise])
        async with self.factory() as session:
            found = await session.scalar(
                text("SELECT id FROM exercises WHERE name = :name AND owner_user_id IS NULL"),
                {"name": name},
            )
        assert isinstance(found, UUID)
        return found

    async def howto(
        self, exercise_id: UUID, status: str, body: str = _SHORT, instructions: str = _ORIGINAL
    ) -> None:
        """A stored how-to with `status`; a flagged one flagged at `FLAGGED_AT`."""
        repository = SqlAlchemyExerciseSimpleTextRepository(self.factory)
        assert await repository.write(
            NewSimpleText(
                exercise_id=exercise_id,
                lang="en",
                text=body,
                template_id="exercise.simple_howto",
                template_version=1,
                model="test/model",
                source_sha256=source_sha256(instructions),
                check_version=1,
                created_at=NOW,
            )
        )
        async with self.factory() as session:
            await session.execute(
                text(
                    "UPDATE exercise_simple_texts SET status = :status, flagged_at = :at, "
                    "check_failures = :failures WHERE exercise_id = :id"
                ),
                {
                    "status": status,
                    "at": FLAGGED_AT if status == "flagged" else None,
                    "failures": 2 if status == "rejected" else 0,
                    "id": exercise_id,
                },
            )
            await session.commit()

    async def row(self, exercise_id: UUID) -> dict[str, object] | None:
        async with self.factory() as session:
            found = (
                await session.execute(
                    text(
                        "SELECT text, status, edited_at FROM exercise_simple_texts "
                        "WHERE exercise_id = :id AND lang = 'en'"
                    ),
                    {"id": exercise_id},
                )
            ).one_or_none()
        return None if found is None else dict(found._mapping)


def _draft(body: str) -> SimpleHowtoDraft:
    return SimpleHowtoDraft(
        text=body, template_id="exercise.simple_howto", template_version=1, model="test/model"
    )


async def _page(factory: SessionFactory, sub: str, *, admin: bool = True) -> Page:
    users = SqlAlchemyUsersRepository(factory)
    sessions = SqlAlchemySessionsRepository(factory)
    clock = Clock()
    user = await users.create(
        NewUser(
            sub=sub, email=f"{sub}@example.com", name=sub, locale="en", status=UserStatus.ACTIVE
        )
    )
    session = await sessions.create(
        NewSession(user_id=user.id, expires_at=clock.now() + timedelta(days=1))
    )
    templates = build_templates()
    app = FastAPI()
    app.include_router(
        build_admin_router(
            templates=templates,
            users=users,
            sessions=sessions,
            status_changer=SqlAlchemyUserStatusChanger(factory),
            tokens=memory_admin_tokens(),
            exercise_media=memory_exercise_media_review(),
            exercise_howtos=HowtoReview(
                texts=SqlAlchemyExerciseSimpleTextRepository(factory),
                clock=clock,
                max_chars=MAX_CHARS,
            ),
            **knowledge_wiring(),
        )
    )
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=users,
        sessions=sessions,
        clock=clock,
        templates=templates,
        admin_emails=[user.email] if admin else [],
    )
    app.add_middleware(CsrfMiddleware, secret_key=SECRET, templates=templates)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
    match = _CSRF_TOKEN_IN_HTML.search(client.get("/admin/exercises").text)
    assert match is not None
    return Page(client=client, factory=factory, csrf=match.group(1))


def _card(html: str, exercise_id: UUID) -> str:
    match = re.search(
        rf'<section[^>]*data-exercise="{exercise_id}".*?</section>', html, flags=re.DOTALL
    )
    assert match is not None, html
    return match.group(0)


def _name() -> str:
    return f"Zz Lateral Raise {uuid4().hex[:6]}"


async def test_howtos_view_lists_flagged_and_rejected_with_status_and_original(
    db_session_factory: SessionFactory,
) -> None:
    page = await _page(db_session_factory, "howtos-list")
    flagged_name, rejected_name, shown_name = _name(), _name(), _name()
    flagged = await page.exercise(flagged_name)
    rejected = await page.exercise(rejected_name)
    shown = await page.exercise(shown_name)
    await page.howto(flagged, "flagged")
    await page.howto(rejected, "rejected", body="Rest the bar on your back and sit down.")
    await page.howto(shown, "shown")

    response = page.client.get("/admin/exercises?view=howtos")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    flagged_card = _card(response.text, flagged)
    assert flagged_name in flagged_card
    assert _SHORT in flagged_card
    assert "Flagged by an athlete · 12 Nov" in flagged_card
    assert "Original instructions" in flagged_card
    assert _ORIGINAL in flagged_card
    assert f"/admin/exercises/howtos/{flagged}/clear" in flagged_card
    rejected_card = _card(response.text, rejected)
    assert "Rejected by the check" in rejected_card
    assert "Rest the bar on your back and sit down." in rejected_card
    assert shown_name not in response.text
    # The chip counts what the list draws (the shared database may hold other rows).
    cards = response.text.count("data-howto=")
    assert f"How-tos · {cards}" in response.text
    assert re.search(r'aria-pressed="true"\s+hx-get="/admin/exercises\?view=howtos"', response.text)


async def test_the_chip_shows_under_the_picture_filters_too(
    db_session_factory: SessionFactory,
) -> None:
    page = await _page(db_session_factory, "howtos-chip")
    await page.howto(await page.exercise(_name()), "flagged")

    response = page.client.get("/admin/exercises", headers={"HX-Request": "true"})

    assert re.search(r"How-tos · [1-9]", response.text)
    assert "data-howto=" not in response.text


async def test_admin_edit_shows_on_card_and_survives_source_change(
    db_session_factory: SessionFactory,
) -> None:
    page = await _page(db_session_factory, "howtos-edit")
    exercise_id = await page.exercise(_name())
    await page.howto(exercise_id, "flagged")
    editing = page.client.get(f"/admin/exercises?view=howtos&edit={exercise_id}")
    assert "Your edit is kept as written and is never regenerated." in _card(
        editing.text, exercise_id
    )

    response = page.client.post(
        f"/admin/exercises/howtos/{exercise_id}/edit",
        headers=page.headers(),
        data={"text": "  Lift both arms out to the sides, then lower them slowly.  "},
    )

    assert response.status_code == 200
    assert f'data-exercise="{exercise_id}"' not in response.text
    edited = "Lift both arms out to the sides, then lower them slowly."
    assert await page.row(exercise_id) == {"text": edited, "status": "shown", "edited_at": NOW}
    card = page.card(exercise_id)
    assert await card.howtos([exercise_id]) == {exercise_id: ShortHowto(text=edited)}
    async with page.factory() as session:
        await session.execute(
            text(
                "UPDATE exercise_texts SET instructions = :new "
                "WHERE exercise_id = :id AND lang = 'en'"
            ),
            {"new": "A new import's instructions for this exercise.", "id": exercise_id},
        )
        await session.commit()
    assert await card.howtos([exercise_id]) == {exercise_id: ShortHowto(text=edited)}
    writer = ScriptedWriter(_draft("A model's text."))
    write = WriteSimpleHowto(
        texts=SqlAlchemyExerciseSimpleTextRepository(page.factory),
        writer=writer,
        check=ScriptedCheck(),
        clock=Clock(),
        max_chars=MAX_CHARS,
    )
    assert await write.write(user_id=uuid4(), exercise_id=exercise_id) == HowtoPresent()
    assert writer.calls == []


async def test_cleared_howto_is_rewritten_by_next_card(
    db_session_factory: SessionFactory,
) -> None:
    page = await _page(db_session_factory, "howtos-clear")
    name = _name()
    exercise_id = await page.exercise(name)
    await page.howto(exercise_id, "rejected")

    response = page.client.post(
        f"/admin/exercises/howtos/{exercise_id}/clear", headers=page.headers()
    )

    assert response.status_code == 200
    assert f"Cleared from {name}." in response.text
    assert "The next card rewrites it." in response.text
    assert 'aria-label="Dismiss"' in response.text
    assert "Undo" not in response.text
    assert f'data-exercise="{exercise_id}"' not in response.text
    assert await page.row(exercise_id) is None
    card = page.card(exercise_id)
    assert await card.howtos([exercise_id]) == {exercise_id: LoadHowto()}
    writer = ScriptedWriter(_draft("Lift your arms out to the sides."))
    write = WriteSimpleHowto(
        texts=SqlAlchemyExerciseSimpleTextRepository(page.factory),
        writer=writer,
        check=ScriptedCheck(),
        clock=Clock(),
        max_chars=MAX_CHARS,
    )
    assert await write.write(user_id=uuid4(), exercise_id=exercise_id) == HowtoStored(
        text="Lift your arms out to the sides."
    )
    stored = await page.row(exercise_id)
    assert stored is not None
    assert stored["status"] == "shown"
    assert stored["text"] == "Lift your arms out to the sides."


@pytest.mark.parametrize(
    ("typed", "message"),
    [
        ("", "Write the how-to before saving."),
        ("   ", "Write the how-to before saving."),
        ("x" * 181, "Keep it to 180 characters or fewer: this one has 181."),
    ],
    ids=["empty", "whitespace", "over-long"],
)
async def test_howto_edit_refuses_empty_and_over_long(
    db_session_factory: SessionFactory, typed: str, message: str
) -> None:
    page = await _page(db_session_factory, f"howtos-refuse-{len(typed)}")
    exercise_id = await page.exercise(_name())
    await page.howto(exercise_id, "flagged")
    before = await page.row(exercise_id)

    response = page.client.post(
        f"/admin/exercises/howtos/{exercise_id}/edit",
        headers=page.headers(),
        data={"text": typed},
    )

    assert response.status_code == 422
    card = _card(response.text, exercise_id)
    assert message in card
    assert 'aria-invalid="true"' in card
    assert f">{typed}</textarea>" in card
    assert await page.row(exercise_id) == before


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "?view=howtos"), ("post", "/edit"), ("post", "/clear")],
    ids=["list", "edit", "clear"],
)
async def test_howto_routes_forbid_non_admin(
    db_session_factory: SessionFactory, method: str, path: str
) -> None:
    page = await _page(db_session_factory, f"howtos-forbid-{path.strip('/?=')}", admin=False)
    exercise_id = await page.exercise(_name())
    await page.howto(exercise_id, "flagged")
    before = await page.row(exercise_id)
    url = (
        f"/admin/exercises{path}"
        if method == "get"
        else f"/admin/exercises/howtos/{exercise_id}{path}"
    )

    response = page.client.request(
        method, url, headers=page.headers(), data={"text": "Hacked."} if method == "post" else None
    )

    assert response.status_code == 403
    assert _SHORT not in response.text
    assert await page.row(exercise_id) == before


@pytest.mark.parametrize("action", ["edit", "clear"])
async def test_howto_writes_refuse_without_csrf(
    db_session_factory: SessionFactory, action: str
) -> None:
    page = await _page(db_session_factory, f"howtos-csrf-{action}")
    exercise_id = await page.exercise(_name())
    await page.howto(exercise_id, "flagged")
    before = await page.row(exercise_id)

    response = page.client.post(
        f"/admin/exercises/howtos/{exercise_id}/{action}",
        headers={"HX-Request": "true"},
        data={"text": "Lift your arms."},
    )

    assert response.status_code == 403
    assert await page.row(exercise_id) == before


@pytest.mark.parametrize("action", ["edit", "clear"])
async def test_howto_writes_leave_a_shown_row_alone_and_are_not_found(
    db_session_factory: SessionFactory, action: str
) -> None:
    page = await _page(db_session_factory, f"howtos-shown-{action}")
    exercise_id = await page.exercise(_name())
    await page.howto(exercise_id, "shown")
    before = await page.row(exercise_id)

    response = page.client.post(
        f"/admin/exercises/howtos/{exercise_id}/{action}",
        headers=page.headers(),
        data={"text": "Lift your arms."},
    )

    assert response.status_code == 404
    assert await page.row(exercise_id) == before


async def test_clearing_an_already_cleared_or_malformed_id_is_not_found(
    db_session_factory: SessionFactory,
) -> None:
    page = await _page(db_session_factory, "howtos-twice")
    exercise_id = await page.exercise(_name())
    await page.howto(exercise_id, "flagged")
    url = f"/admin/exercises/howtos/{exercise_id}/clear"
    assert page.client.post(url, headers=page.headers()).status_code == 200

    again = page.client.post(url, headers=page.headers())
    malformed = page.client.post("/admin/exercises/howtos/nope/clear", headers=page.headers())
    no_text = page.client.post("/admin/exercises/howtos/nope/edit", headers=page.headers(), data={})

    assert [again.status_code, malformed.status_code, no_text.status_code] == [404] * 3


async def test_an_unknown_edit_id_draws_the_list(db_session_factory: SessionFactory) -> None:
    page = await _page(db_session_factory, "howtos-unknown-edit")
    exercise_id = await page.exercise(_name())
    await page.howto(exercise_id, "flagged")

    for edit in (str(uuid4()), "nope"):
        response = page.client.get(f"/admin/exercises?view=howtos&edit={edit}")

        assert response.status_code == 200
        assert "data-howto-edit>" not in response.text
        assert "<textarea" not in response.text
        assert _SHORT in _card(response.text, exercise_id)


async def test_a_flagged_howto_whose_exercise_lost_its_english_text_is_still_listed(
    db_session_factory: SessionFactory,
) -> None:
    # The row stays hidden from athletes while flagged, so the admin must still reach it.
    page = await _page(db_session_factory, "howtos-no-original")
    exercise_id = await page.exercise(_name())
    await page.howto(exercise_id, "flagged")
    async with page.factory() as session:
        await session.execute(
            text("DELETE FROM exercise_texts WHERE exercise_id = :id AND lang = 'en'"),
            {"id": exercise_id},
        )
        await session.commit()

    response = page.client.get("/admin/exercises?view=howtos")

    assert response.status_code == 200
    card = _card(response.text, exercise_id)
    assert _SHORT in card
    assert "data-howto-original" not in card
    cleared = page.client.post(
        f"/admin/exercises/howtos/{exercise_id}/clear", headers=page.headers()
    )
    assert cleared.status_code == 200


async def test_review_rows_leave_out_text_less_rows(db_session_factory: SessionFactory) -> None:
    page = await _page(db_session_factory, "howtos-textless")
    exercise_id = await page.exercise(_name())
    repository = SqlAlchemyExerciseSimpleTextRepository(page.factory)
    await page.howto(exercise_id, "flagged")
    async with page.factory() as session:
        # A shown row whose first check failed holds no text, and is never listed.
        await session.execute(
            text(
                "UPDATE exercise_simple_texts SET text = NULL, status = 'shown', "
                "check_failures = 1, flagged_at = NULL WHERE exercise_id = :id"
            ),
            {"id": exercise_id},
        )
        await session.commit()

    rows = await repository.review_rows(lang="en")

    assert exercise_id not in {row.exercise_id for row in rows}
