"""The AI onboarding's routes (PRD-0007 B33-B36, F6, F20, F21, ADR-0028, ticket #254): the sport
card, About you with Skip and Back, the summary stream and confirm. `user_id` comes only from
the session, and a send answers before its read does."""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import JsonValue

from ai_trainer.application.background_reads import BackgroundReads
from ai_trainer.domain.athlete_profile import AthleteProfile
from ai_trainer.domain.onboarding import AnswerStatus, OnboardingStep
from ai_trainer.domain.sessions import Session
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.domain.users import User, UserStatus
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.ai_onboarding import build_ai_onboarding_router
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.templating import build_templates
from tests.chat_support import include_chat
from tests.onboarding_support import FakeOnboardingAnswers, FakeReader
from tests.unit.test_chat_reply_route import _sse_data
from tests.unit.test_onboarding_route import FakeSessionsRepository, FakeUsersRepository

NOW = datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
ABOUT_YOU = OnboardingStep.ABOUT_YOU
AC_READ: dict[str, JsonValue] = {
    "age": 31,
    "height_cm": 178,
    "weight_kg": 73.0,
    "limits": "Sore left shoulder",
}
HX = {"HX-Request": "true"}


class FakeClock:
    def now(self) -> datetime:
        return NOW


class SyncedUsers(FakeUsersRepository):
    """The gate re-reads the user on every request; this one sees what the answers fake wrote
    to the user's row (its timezone and `onboarded_at`)."""

    def __init__(self, user: User, answers: FakeOnboardingAnswers) -> None:
        super().__init__(user)
        self._answers = answers

    async def get(self, user_id: UUID) -> User | None:
        user = await super().get(user_id)
        if user is None:
            return None
        return user.model_copy(
            update={
                "timezone": self._answers.timezones.get(user_id, user.timezone),
                "onboarded_at": self._answers.onboarded_at.get(user_id, user.onboarded_at),
            }
        )


@dataclass
class Athlete:
    client: TestClient
    user: User
    answers: FakeOnboardingAnswers
    reader: FakeReader
    reads: BackgroundReads


def _user(*, onboarded: bool = False) -> User:
    return User(
        id=uuid4(),
        sub=f"sub-{uuid4()}",
        email="athlete@example.com",
        name="Athlete",
        locale="en",
        status=UserStatus.ACTIVE,
        created_at=NOW,
        onboarded_at=NOW if onboarded else None,
    )


@pytest.fixture
def athlete() -> Iterator[Athlete]:
    yield from _athlete()


def _athlete(
    *,
    user: User | None = None,
    answers: FakeOnboardingAnswers | None = None,
    reader: FakeReader | None = None,
) -> Iterator[Athlete]:
    user = user or _user()
    answers = answers or FakeOnboardingAnswers()
    reader = reader or FakeReader(AC_READ)
    reads = BackgroundReads()
    session = Session(
        id=uuid4(), user_id=user.id, expires_at=NOW + timedelta(days=1), created_at=NOW
    )
    templates = build_templates()
    app = FastAPI()
    include_chat(app, templates)
    app.include_router(
        build_ai_onboarding_router(
            templates,
            registry=default_sport_registry(),
            answers=answers,
            reads=reads,
            reader=reader,
            clock=FakeClock(),
        )
    )
    app.add_middleware(
        ActiveUserGateMiddleware,
        users=SyncedUsers(user, answers),
        sessions=FakeSessionsRepository(session),
        clock=FakeClock(),
        templates=templates,
        admin_emails=[],
    )
    # One event loop for the whole test, so a read started by one request is still held when
    # the next arrives, as in the running app.
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
        yield Athlete(client, user, answers, reader, reads)


async def _picked(athlete: Athlete, sport: str = "climbing", zone: str = "Europe/Warsaw") -> None:
    await athlete.answers.start(athlete.user.id, sport, zone)


async def _answered(athlete: Athlete, read: dict[str, JsonValue] | None = AC_READ) -> None:
    await _picked(athlete)
    revision = await athlete.answers.store_answer(
        athlete.user.id, ABOUT_YOU, None, "31, 5 ft 10, 160 lb, sore left shoulder"
    )
    if read is not None:
        await athlete.answers.save_read(athlete.user.id, ABOUT_YOU, None, revision, read)


def _summary(athlete: Athlete) -> str:
    response = athlete.client.get("/onboarding/summary/stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    [event] = _sse_data(response.text)
    return event


def _value(html: str, name: str) -> str | None:
    match = re.search(rf'<input[^>]*name="{name}"[^>]*>', html)
    assert match is not None, name
    value = re.search(r'value="([^"]*)"', match.group(0))
    return value.group(1) if value else None


# --- Entry and the sport card --------------------------------------------------------------


def test_onboarding_opens_at_the_sport_card(athlete: Athlete) -> None:
    response = athlete.client.get("/onboarding", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding/sport"


def test_a_finished_athlete_is_sent_home_from_every_card() -> None:
    for athlete in _athlete(user=_user(onboarded=True)):
        for path in ("/onboarding", "/onboarding/sport", "/onboarding/about-you"):
            response = athlete.client.get(path, follow_redirects=False)
            assert response.headers["location"] == "/", path
        response = athlete.client.post("/onboarding/confirm", data={}, follow_redirects=False)
        assert response.headers["location"] == "/"


def test_the_sport_card_lists_every_sport_with_its_icon_and_says_only_it_is_required(
    athlete: Athlete,
) -> None:
    response = athlete.client.get("/onboarding/sport")

    assert response.status_code == 200
    assert "<html" in response.text
    assert "Pick a sport to start with" in response.text
    assert "Only the sport is required" in response.text
    for sport in ("climbing", "gym", "cycling"):
        # htmx 4 sends neither a button's own name nor value, so the pick rides in hx-vals.
        assert f'hx-vals=\'{{"sport": "{sport}"}}\'' in response.text
    for label in ("Climbing", "Gym", "Cycling"):
        assert label in response.text
    assert response.text.count("<svg") >= 6
    assert 'name="timezone"' in response.text
    assert "/static/js/onboarding_zone.js" in response.text


def test_the_sport_card_with_hx_request_is_a_partial(athlete: Athlete) -> None:
    response = athlete.client.get("/onboarding/sport", headers=HX)
    assert "<html" not in response.text
    assert 'name="sport" value="climbing"' in response.text


async def test_picking_a_sport_stores_it_with_the_browser_zone_and_opens_about_you(
    athlete: Athlete,
) -> None:
    response = athlete.client.post(
        "/onboarding/sport",
        data={"sport": "climbing", "timezone": "Europe/Warsaw"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding/about-you"
    assert athlete.answers.sports[athlete.user.id] == ["climbing"]
    assert athlete.answers.timezones[athlete.user.id] == "Europe/Warsaw"


def test_an_htmx_pick_moves_on_with_hx_redirect(athlete: Athlete) -> None:
    response = athlete.client.post(
        "/onboarding/sport", data={"sport": "gym", "timezone": ""}, headers=HX
    )
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/onboarding/about-you"
    assert athlete.answers.timezones[athlete.user.id] == "UTC"


@pytest.mark.parametrize("data", [{"sport": "chess"}, {}])
def test_an_unknown_or_missing_sport_is_rejected(athlete: Athlete, data: dict[str, str]) -> None:
    response = athlete.client.post("/onboarding/sport", data=data, follow_redirects=False)
    assert response.status_code == 422
    assert athlete.answers.sports == {}


# --- About you -----------------------------------------------------------------------------


def test_about_you_needs_a_sport_first(athlete: Athlete) -> None:
    response = athlete.client.get("/onboarding/about-you", follow_redirects=False)
    assert response.headers["location"] == "/onboarding/sport"


async def test_about_you_asks_its_question_with_skip_back_and_optional(athlete: Athlete) -> None:
    await _picked(athlete)

    response = athlete.client.get("/onboarding/about-you")

    assert response.status_code == 200
    assert "Tell me about yourself" in response.text
    assert "Optional" in response.text
    for hint in ("Age", "Height", "Weight", "Injuries"):
        assert f">{hint}<" in response.text
    assert 'hx-post="/onboarding/about-you/skip"' in response.text
    assert 'href="/onboarding/sport"' in response.text
    assert 'name="answer"' in response.text


async def test_the_next_step_appears_before_the_model_call_returns() -> None:
    """AC: the next step appears before the model call returns. The read never returns here,
    yet the send answers with the summary."""
    reader = FakeReader(AC_READ)
    reader.hold = True
    for athlete in _athlete(reader=reader):
        await _picked(athlete)

        response = athlete.client.post(
            "/onboarding/about-you",
            data={"answer": "31, 5 ft 10, 160 lb, sore left shoulder"},
            headers=HX,
        )

        assert response.headers["HX-Redirect"] == "/onboarding/summary"
        [answer] = await athlete.answers.list_answers(athlete.user.id)
        assert answer.status is AnswerStatus.PENDING
        assert athlete.reads.running((athlete.user.id, ABOUT_YOU, None)) is not None


async def test_a_blank_answer_shows_the_card_again_with_an_error(athlete: Athlete) -> None:
    await _picked(athlete)

    response = athlete.client.post("/onboarding/about-you", data={"answer": "  "}, headers=HX)

    assert response.status_code == 422
    assert "<html" not in response.text
    assert 'aria-invalid="true"' in response.text
    assert await athlete.answers.list_answers(athlete.user.id) == []


async def test_skip_leaves_about_you_empty_and_moves_on(athlete: Athlete) -> None:
    await _picked(athlete)

    response = athlete.client.post("/onboarding/about-you/skip", headers=HX)

    assert response.headers["HX-Redirect"] == "/onboarding/summary"
    [answer] = await athlete.answers.list_answers(athlete.user.id)
    assert answer.status is AnswerStatus.SKIPPED
    assert athlete.reader.calls == []


async def test_back_to_about_you_shows_the_earlier_answer_to_change(athlete: Athlete) -> None:
    await _answered(athlete)

    response = athlete.client.get("/onboarding/about-you")

    assert "31, 5 ft 10, 160 lb, sore left shoulder</textarea>" in response.text


async def test_a_reload_resumes_at_the_next_unanswered_card(athlete: Athlete) -> None:
    await _picked(athlete)
    response = athlete.client.get("/onboarding", follow_redirects=False)
    assert response.headers["location"] == "/onboarding/about-you"

    await athlete.answers.skip_answer(athlete.user.id, ABOUT_YOU, None)
    response = athlete.client.get("/onboarding", follow_redirects=False)
    assert response.headers["location"] == "/onboarding/summary"


# --- The summary ---------------------------------------------------------------------------


async def test_the_summary_needs_about_you_first(athlete: Athlete) -> None:
    await _picked(athlete)
    response = athlete.client.get("/onboarding/summary", follow_redirects=False)
    assert response.headers["location"] == "/onboarding/about-you"


async def test_the_summary_page_is_a_skeleton_that_owns_the_stream(athlete: Athlete) -> None:
    await _answered(athlete)

    response = athlete.client.get("/onboarding/summary")

    assert response.status_code == 200
    assert 'hx-sse:connect="/onboarding/summary/stream"' in response.text
    assert 'hx-config="sse.reconnect:false sse.pauseOnBackground:false"' in response.text
    assert "skeleton" in response.text
    assert 'role="status"' in response.text


async def test_the_ac_answer_shows_31_178_cm_73_kg_and_the_shoulder(athlete: Athlete) -> None:
    """AC: "31, 5 ft 10, 160 lb, sore left shoulder" shows 31, 178 cm, 73 kg, shoulder under
    limits."""
    await _answered(athlete, read=None)

    event = _summary(athlete)

    assert _value(event, "age") == "31"
    assert _value(event, "height_cm") == "178"
    assert _value(event, "weight_kg") == "73"
    assert _value(event, "limits") == "Sore left shoulder"
    assert '<option value="Europe/Warsaw" selected>' in event
    assert 'hx-target="#onboarding-summary"' in event


async def test_skip_shows_about_you_empty_and_skipped(athlete: Athlete) -> None:
    """AC: Skip leaves About you empty and "Skipped"."""
    await _picked(athlete)
    await athlete.answers.skip_answer(athlete.user.id, ABOUT_YOU, None)

    event = _summary(athlete)

    assert "Skipped" in event
    for field in ("age", "height_cm", "weight_kg"):
        assert _value(event, field) in (None, "")
    assert 'placeholder="Not mentioned"' in event
    assert athlete.reader.calls == []


async def test_an_answered_step_is_not_marked_skipped(athlete: Athlete) -> None:
    await _answered(athlete)
    assert "Skipped" not in _summary(athlete)


async def test_a_restart_mid_read_still_fills_the_summary(athlete: Athlete) -> None:
    """AC: a restart mid-read still fills the summary. The answer is pending and no task is
    held for it, as after a restart; the stream reads it itself."""
    await _answered(athlete, read=None)
    assert athlete.reads.running((athlete.user.id, ABOUT_YOU, None)) is None

    event = _summary(athlete)

    assert athlete.reader.calls == [
        (athlete.user.id, "31, 5 ft 10, 160 lb, sore left shoulder", "climbing", "Europe/Warsaw")
    ]
    assert _value(event, "height_cm") == "178"


async def test_a_read_that_fails_twice_still_opens_the_summary() -> None:
    for athlete in _athlete(reader=FakeReader(None)):
        await _answered(athlete, read=None)
        await athlete.answers.mark_failed(athlete.user.id, ABOUT_YOU, None, 1)

        event = _summary(athlete)

        assert _value(event, "age") in (None, "")
        assert "Skipped" not in event
        assert len(athlete.reader.calls) == 1


async def test_another_athletes_answers_are_never_shown() -> None:
    """AC: another athlete's answers are never shown."""
    answers = FakeOnboardingAnswers()
    other = uuid4()
    await answers.start(other, "climbing", "UTC")
    revision = await answers.store_answer(other, ABOUT_YOU, None, "31")
    await answers.save_read(other, ABOUT_YOU, None, revision, AC_READ)

    for athlete in _athlete(answers=answers):
        await _picked(athlete)
        await answers.skip_answer(athlete.user.id, ABOUT_YOU, None)

        event = _summary(athlete)

        assert "Sore left shoulder" not in event
        assert _value(event, "age") in (None, "")
        assert athlete.reader.calls == []
        athlete.client.post("/onboarding/confirm", data={"timezone": "UTC"}, headers=HX)
        assert len(await answers.list_answers(other)) == 1
        assert other not in answers.profiles


# --- Confirm -------------------------------------------------------------------------------


async def test_a_corrected_field_is_saved_and_no_answers_remain(athlete: Athlete) -> None:
    """AC: a corrected field is saved; no answers remain after confirm."""
    await _answered(athlete)

    response = athlete.client.post(
        "/onboarding/confirm",
        data={
            "age": "31",
            "height_cm": "178",
            "weight_kg": "75",
            "limits": "Sore left shoulder",
            "timezone": "Europe/Warsaw",
        },
        headers=HX,
    )

    assert response.headers["HX-Redirect"] == "/"
    assert athlete.answers.profiles[athlete.user.id] == AthleteProfile(
        birth_year=1995, height_cm=178, weight_kg=75, limits="Sore left shoulder"
    )
    assert athlete.answers.onboarded_at[athlete.user.id] == NOW
    assert await athlete.answers.list_answers(athlete.user.id) == []


async def test_a_bad_field_shows_the_summary_again_with_it_flagged(athlete: Athlete) -> None:
    await _answered(athlete)

    response = athlete.client.post(
        "/onboarding/confirm",
        data={"age": "31", "height_cm": "20", "weight_kg": "", "timezone": "Europe/Warsaw"},
        headers=HX,
    )

    assert response.status_code == 422
    assert "<html" not in response.text
    height = re.search(r'<input[^>]*name="height_cm"[^>]*>', response.text)
    assert height is not None
    assert 'aria-invalid="true"' in height.group(0)
    assert 'value="20"' in height.group(0)
    assert _value(response.text, "age") == "31"
    assert athlete.answers.profiles == {}
    assert len(await athlete.answers.list_answers(athlete.user.id)) == 1


def test_the_old_sports_step_is_gone(athlete: Athlete) -> None:
    assert athlete.client.get("/onboarding/sports").status_code == 404


def test_a_post_to_a_card_not_yet_due_is_sent_to_the_card_that_is(athlete: Athlete) -> None:
    for path in ("/onboarding/about-you", "/onboarding/about-you/skip", "/onboarding/confirm"):
        response = athlete.client.post(path, data={"answer": "31"}, follow_redirects=False)
        assert response.headers["location"] == "/onboarding/sport", path
    assert athlete.answers.answers == {}


def test_a_finished_athlete_cannot_pick_again() -> None:
    for athlete in _athlete(user=_user(onboarded=True)):
        response = athlete.client.post(
            "/onboarding/sport", data={"sport": "gym"}, follow_redirects=False
        )
        assert response.headers["location"] == "/"
        assert athlete.answers.sports == {}


async def test_a_weight_with_a_decimal_comma_is_saved_to_one_decimal(athlete: Athlete) -> None:
    await _answered(athlete)

    response = athlete.client.post(
        "/onboarding/confirm", data={"weight_kg": "72,5", "timezone": "UTC"}, headers=HX
    )

    assert response.headers["HX-Redirect"] == "/"
    assert athlete.answers.profiles[athlete.user.id].weight_kg == 72.5


async def test_a_weight_with_two_decimals_is_flagged_with_why(athlete: Athlete) -> None:
    await _answered(athlete)

    response = athlete.client.post(
        "/onboarding/confirm", data={"weight_kg": "72.25", "timezone": "UTC"}, headers=HX
    )

    assert response.status_code == 422
    assert "at most one decimal" in response.text
