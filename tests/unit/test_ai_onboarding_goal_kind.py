"""The goal kind on the onboarding summary (PRD 0013 B45, F21, ADR-0028 #618 amendment, ADR-0034,
ticket #628): each goal shows Jev's pick as a chip the athlete can change or clear, confirm saves
what is shown, and a kind changed from Jev's pick records one content-free `decision_overridden`
event."""

import re
from collections.abc import Iterator
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import JsonValue

from ai_trainer.application.background_reads import BackgroundReads
from ai_trainer.application.feedback import FeedbackRecorder
from ai_trainer.application.goal_kind_overrides import overridden_kinds
from ai_trainer.application.ports.draft_settling import DecisionRef
from ai_trainer.domain.feedback import FeedbackKind
from ai_trainer.domain.goals import Goal, GoalKind
from ai_trainer.domain.onboarding import OnboardingStep
from ai_trainer.domain.sessions import Session
from ai_trainer.domain.sport_profile import SportForm, SportSection
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.web.active_user_gate import ActiveUserGateMiddleware
from ai_trainer.web.ai_onboarding import build_ai_onboarding_router
from ai_trainer.web.auth import SESSION_COOKIE_NAME
from ai_trainer.web.templating import build_templates
from tests.chat_support import include_chat
from tests.feedback_support import InMemoryFeedbackEvents
from tests.onboarding_support import (
    FakeOnboardingAnswers,
    FakeReader,
    FakeSessionsRepository,
)
from tests.unit.test_ai_onboarding_route import (
    HX,
    NOW,
    Athlete,
    FakeClock,
    SyncedUsers,
    _picked,
    _summary,
    _user,
)

GOAL_TEXT = "Touch my toes by summer"
DECISION = DecisionRef("goal_kind", 1, ("kind",))


@pytest.fixture
def events() -> InMemoryFeedbackEvents:
    return InMemoryFeedbackEvents()


@pytest.fixture
def athlete(events: InMemoryFeedbackEvents) -> Iterator[Athlete]:
    user, answers = _user(), FakeOnboardingAnswers()
    reader, reads = FakeReader({}), BackgroundReads()
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
            feedback=FeedbackRecorder(events, clock=FakeClock()),
            goal_kind_decision=DECISION,
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
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, str(session.id))
        yield Athlete(client, user, answers, reader, reads)


async def _with_goal(athlete: Athlete, kind: str | None) -> None:
    await _picked(athlete, "gym")
    await athlete.answers.skip_answer(athlete.user.id, OnboardingStep.ABOUT_YOU, None)
    goal: dict[str, JsonValue] = {"text": GOAL_TEXT}
    if kind is not None:
        goal["kind"] = kind
    for step in (OnboardingStep.GOALS, OnboardingStep.LEVEL, OnboardingStep.SCHEDULE):
        revision = await athlete.answers.store_answer(athlete.user.id, step, "gym", "x")
        read: dict[str, JsonValue] = {"goals": [goal]} if step is OnboardingStep.GOALS else {}
        await athlete.answers.save_read(athlete.user.id, step, "gym", revision, read)


def _confirm(athlete: Athlete, kind: str | None) -> None:
    data = {"timezone": "UTC", "gym.goal_text": GOAL_TEXT, "gym.goal_date": ""}
    if kind is not None:
        data["gym.goal_kind.0"] = kind
    response = athlete.client.post("/onboarding/confirm", data=data, headers=HX)
    assert response.headers["HX-Redirect"] == "/"


def _chips(html: str) -> dict[str, bool]:
    """Each kind chip of the summary's goal, and whether it is checked."""
    return {
        match.group(1): "checked" in match.group(0)
        for match in re.finditer(
            r'<input[^>]*name="gym\.goal_kind\.0"[^>]*value="(\w+)"[^>]*>', html
        )
    }


def _kind_group(html: str) -> str:
    return html.split('aria-label="Kind"')[1].split("</fieldset>")[0]


async def test_summary_shows_goal_kind_picked_chip(athlete: Athlete) -> None:
    """AC: a goal read as mobility shows Mobility."""
    await _with_goal(athlete, "mobility")

    event = _summary(athlete)

    assert _chips(event) == {
        "strength": False,
        "muscle": False,
        "endurance": False,
        "weight_loss": False,
        "mobility": True,
        "general_fitness": False,
    }
    assert ">Mobility<" in event
    assert "Not mentioned" not in _kind_group(event)


async def test_summary_shows_not_mentioned_for_empty_kind(athlete: Athlete) -> None:
    """AC edge: an empty kind shows "Not mentioned" and no chip is picked."""
    await _with_goal(athlete, None)

    event = _summary(athlete)

    assert not any(_chips(event).values())
    assert "Not mentioned" in _kind_group(event)


async def test_confirm_stores_the_changed_goal_kind(athlete: Athlete) -> None:
    """AC: changing the kind to General fitness and confirming stores it."""
    await _with_goal(athlete, "mobility")

    _confirm(athlete, "general_fitness")

    assert [goal.kind for goal in athlete.answers.goals[athlete.user.id]] == [
        GoalKind.GENERAL_FITNESS
    ]


async def test_confirm_empty_kind_stores_none(
    athlete: Athlete, events: InMemoryFeedbackEvents
) -> None:
    """AC edge: an empty kind confirms as empty, and Jev's pick of none is not overridden."""
    await _with_goal(athlete, None)

    _confirm(athlete, None)

    assert [goal.kind for goal in athlete.answers.goals[athlete.user.id]] == [None]
    assert events.events == []


async def test_confirm_records_one_override_event_for_a_changed_kind(
    athlete: Athlete, events: InMemoryFeedbackEvents
) -> None:
    """AC: one content-free `decision_overridden` event for `goal_kind`."""
    await _with_goal(athlete, "mobility")

    _confirm(athlete, "general_fitness")

    [event] = events.events
    assert event.user_id == athlete.user.id
    assert event.kind is FeedbackKind.DECISION_OVERRIDDEN
    assert (event.template_id, event.template_version, event.fields) == ("goal_kind", 1, ["kind"])
    assert GOAL_TEXT not in event.model_dump_json()
    assert "general_fitness" not in event.model_dump_json()


async def test_clearing_the_kind_records_an_override(
    athlete: Athlete, events: InMemoryFeedbackEvents
) -> None:
    await _with_goal(athlete, "mobility")

    _confirm(athlete, None)

    assert len(events.events) == 1


async def test_confirm_records_nothing_when_kind_kept(
    athlete: Athlete, events: InMemoryFeedbackEvents
) -> None:
    await _with_goal(athlete, "mobility")

    _confirm(athlete, "mobility")

    assert events.events == []


async def test_a_goal_added_on_the_summary_records_no_override(
    athlete: Athlete, events: InMemoryFeedbackEvents
) -> None:
    """Jev picked nothing for a goal it never read."""
    await _with_goal(athlete, "mobility")

    response = athlete.client.post(
        "/onboarding/confirm",
        data={
            "timezone": "UTC",
            "gym.goal_text": [GOAL_TEXT, "Squat 100 kg"],
            "gym.goal_date": ["", ""],
            "gym.goal_kind.0": "mobility",
            "gym.goal_kind.1": "strength",
        },
        headers=HX,
    )

    assert response.headers["HX-Redirect"] == "/"
    assert events.events == []
    assert [goal.kind for goal in athlete.answers.goals[athlete.user.id]] == [
        GoalKind.MOBILITY,
        GoalKind.STRENGTH,
    ]


async def test_a_bad_kind_flags_the_goal_and_records_nothing(
    athlete: Athlete, events: InMemoryFeedbackEvents
) -> None:
    await _with_goal(athlete, "mobility")

    response = athlete.client.post(
        "/onboarding/confirm",
        data={
            "timezone": "UTC",
            "gym.goal_text": GOAL_TEXT,
            "gym.goal_date": "",
            "gym.goal_kind.0": "flexibility",
        },
        headers=HX,
    )

    assert response.status_code == 422
    assert "Choose a kind from the list." in response.text
    assert events.events == []
    assert athlete.user.id not in athlete.answers.goals


def test_a_goal_the_athlete_cleared_is_no_override() -> None:
    read = SportSection(goals=(Goal(text=GOAL_TEXT, kind=GoalKind.MOBILITY),))

    typed = SportForm(goal_texts=("",), goal_kinds=("",))

    assert overridden_kinds(read, typed) == 0
