"""The goals, level and schedule cards and the sport's summary section (PRD 0008 B33-B35, F20,
F21, ADR-0028, ticket #255): each card asks its sport's question with Skip and Back, a send moves
straight on while the read runs, the summary shows what was read under the sport, and confirm
saves it or flags a bad field inline."""

import re
from collections.abc import Iterator
from datetime import date

import pytest
from pydantic import JsonValue

from ai_trainer.domain.goals import Goal
from ai_trainer.domain.onboarding import AnswerStatus, OnboardingStep
from ai_trainer.domain.sport_profile import SportProfile
from tests.onboarding_support import FakeReader
from tests.unit.test_ai_onboarding_route import (
    HX,
    Athlete,
    _athlete,
    _picked,
    _summary,
    _value,
)

ABOUT_YOU, GOALS, LEVEL, SCHEDULE = (
    OnboardingStep.ABOUT_YOU,
    OnboardingStep.GOALS,
    OnboardingStep.LEVEL,
    OnboardingStep.SCHEDULE,
)
GOALS_READ: dict[str, JsonValue] = {
    "goals": [{"text": "Send my first 7c outdoors", "target_date": "2027-06-30"}]
}
LEVEL_READ: dict[str, JsonValue] = {"level": "Deadlift 125 kg", "exercises": "Push pull legs"}
SCHEDULE_READ: dict[str, JsonValue] = {
    "sessions_per_week": 4,
    "session_minutes": 120,
    "preferred_days": [0, 2, 4, 5],
}


@pytest.fixture
def athlete() -> Iterator[Athlete]:
    yield from _athlete()


async def _at_goals(athlete: Athlete, sport: str = "climbing") -> None:
    await _picked(athlete, sport)
    await athlete.answers.skip_answer(athlete.user.id, ABOUT_YOU, None)


async def _read(
    athlete: Athlete, step: OnboardingStep, read: dict[str, JsonValue], sport: str = "climbing"
) -> None:
    revision = await athlete.answers.store_answer(athlete.user.id, step, sport, "x")
    await athlete.answers.save_read(athlete.user.id, step, sport, revision, read)


def _checked_days(html: str, sport: str) -> list[str]:
    return re.findall(
        rf'<input[^>]*name="{re.escape(sport)}\.days"[^>]*value="(\d)"[^>]*checked', html
    )


# --- The cards ----------------------------------------------------------------------------


async def test_about_you_moves_on_to_goals(athlete: Athlete) -> None:
    await _picked(athlete)

    response = athlete.client.post("/onboarding/about-you", data={"answer": "31"}, headers=HX)

    assert response.headers["HX-Redirect"] == "/onboarding/climbing/goals"


async def test_the_goals_card_asks_the_sports_question_with_skip_back_and_progress(
    athlete: Athlete,
) -> None:
    await _at_goals(athlete)

    response = athlete.client.get("/onboarding/climbing/goals")

    assert response.status_code == 200
    assert "What do you want from climbing?" in response.text
    assert "e.g. First 7c outdoors by June" in response.text
    assert "Climbing · Goals" in response.text
    for hint in ("Goal", "Target date"):
        assert f">{hint}</span>" in response.text
    assert 'hx-post="/onboarding/climbing/goals/skip"' in response.text
    assert 'hx-post="/onboarding/climbing/goals"' in response.text
    assert 'href="/onboarding/about-you"' in response.text
    assert 'aria-valuenow="3"' in response.text
    assert 'aria-valuemax="5"' in response.text


async def test_each_card_has_its_own_sports_copy() -> None:
    for each in _athlete():
        await _at_goals(each, "gym")
        await each.answers.skip_answer(each.user.id, GOALS, "gym")

        response = each.client.get("/onboarding/gym/level", headers=HX)

        assert "<html" not in response.text
        assert "Your main lifts and the exercises you do." in response.text
        assert "Gym · Level and exercises" in response.text
        assert 'href="/onboarding/gym/goals"' in response.text


async def test_the_schedule_card_asks_how_often_and_how_long(athlete: Athlete) -> None:
    await _at_goals(athlete)
    for step in (GOALS, LEVEL):
        await athlete.answers.skip_answer(athlete.user.id, step, "climbing")

    response = athlete.client.get("/onboarding/climbing/schedule")

    assert "How often do you want to train, and for how long?" in response.text
    assert 'href="/onboarding/climbing/level"' in response.text
    assert 'aria-valuenow="5"' in response.text


async def test_a_send_moves_straight_on_while_the_read_runs() -> None:
    """Send = Continue (ADR-0028): the read never returns here, yet the next card is due."""
    reader = FakeReader(GOALS_READ)
    reader.hold = True
    for each in _athlete(reader=reader):
        await _at_goals(each)

        response = each.client.post(
            "/onboarding/climbing/goals",
            data={"answer": "Send my first 7c outdoors by June"},
            headers=HX,
        )

        assert response.headers["HX-Redirect"] == "/onboarding/climbing/level"
        answer = next(a for a in await each.answers.list_answers(each.user.id) if a.step is GOALS)
        assert (answer.sport_id, answer.status) == ("climbing", AnswerStatus.PENDING)
        assert each.reads.running((each.user.id, GOALS, "climbing")) is not None


async def test_skip_and_send_lead_through_level_and_schedule_to_another_sport(
    athlete: Athlete,
) -> None:
    await _at_goals(athlete)

    skipped = athlete.client.post("/onboarding/climbing/goals/skip", headers=HX)
    sent = athlete.client.post("/onboarding/climbing/level", data={"answer": "7b"}, headers=HX)
    last = athlete.client.post("/onboarding/climbing/schedule/skip", headers=HX)

    assert skipped.headers["HX-Redirect"] == "/onboarding/climbing/level"
    assert sent.headers["HX-Redirect"] == "/onboarding/climbing/schedule"
    # Two sports are left, so "Add another sport?" comes before the summary (#256).
    assert last.headers["HX-Redirect"] == "/onboarding/more"


async def test_a_blank_answer_shows_the_card_again_with_an_error(athlete: Athlete) -> None:
    await _at_goals(athlete)

    response = athlete.client.post("/onboarding/climbing/goals", data={"answer": " "}, headers=HX)

    assert response.status_code == 422
    assert "<html" not in response.text
    assert 'aria-invalid="true"' in response.text
    assert "What do you want from climbing?" in response.text


async def test_back_shows_the_earlier_answer_to_change(athlete: Athlete) -> None:
    await _at_goals(athlete)
    await athlete.answers.store_answer(athlete.user.id, GOALS, "climbing", "First 7c by June")

    response = athlete.client.get("/onboarding/climbing/goals")

    assert "First 7c by June</textarea>" in response.text


async def test_a_card_not_yet_due_sends_the_athlete_to_the_one_that_is(athlete: Athlete) -> None:
    await _picked(athlete)

    for path in (
        "/onboarding/climbing/goals",
        "/onboarding/climbing/level",
        "/onboarding/climbing/schedule",
    ):
        response = athlete.client.get(path, follow_redirects=False)
        assert response.headers["location"] == "/onboarding/about-you", path

    response = athlete.client.post("/onboarding/climbing/schedule/skip", follow_redirects=False)
    assert response.headers["location"] == "/onboarding/about-you"
    response = athlete.client.post(
        "/onboarding/climbing/goals", data={"answer": "7c"}, follow_redirects=False
    )
    assert response.headers["location"] == "/onboarding/about-you"
    assert len(await athlete.answers.list_answers(athlete.user.id)) == 0


async def test_a_reload_resumes_at_the_next_unanswered_sport_step(athlete: Athlete) -> None:
    await _at_goals(athlete)
    await athlete.answers.skip_answer(athlete.user.id, GOALS, "climbing")

    response = athlete.client.get("/onboarding", follow_redirects=False)

    assert response.headers["location"] == "/onboarding/climbing/level"


# --- The summary --------------------------------------------------------------------------


async def test_the_skeleton_has_a_section_for_the_sport(athlete: Athlete) -> None:
    await _at_goals(athlete)
    for step in (GOALS, LEVEL, SCHEDULE):
        await athlete.answers.skip_answer(athlete.user.id, step, "climbing")

    response = athlete.client.get("/onboarding/summary")

    assert ">Climbing</div>" in response.text


async def test_the_summary_shows_the_goal_with_june_next_year(athlete: Athlete) -> None:
    """AC: "Send my first 7c outdoors by June", sent in October from `Europe/Warsaw`, gets a
    target date in June of the next year."""
    await _at_goals(athlete)
    await _read(athlete, GOALS, GOALS_READ)

    event = _summary(athlete)

    assert _value(event, "climbing.goal_text") == "Send my first 7c outdoors"
    assert _value(event, "climbing.goal_date") == "2027-06-30"


async def test_the_summary_shows_4_sessions_of_120_minutes_on_mon_wed_fri_sat(
    athlete: Athlete,
) -> None:
    """AC: "4 times a week, around 2 hours, Mon Wed Fri Sat" shows 4, 120 min and Mon, Wed,
    Fri, Sat."""
    await _at_goals(athlete)
    await _read(athlete, SCHEDULE, SCHEDULE_READ)

    event = _summary(athlete)

    assert _value(event, "climbing.sessions_per_week") == "4"
    assert _value(event, "climbing.session_minutes") == "120"
    assert _checked_days(event, "climbing") == ["0", "2", "4", "5"]
    for day in ("Mon", "Wed", "Fri", "Sat"):
        assert f"{day}</label>" in event or f">{day}<" in event


async def test_the_summary_puts_the_lift_under_level_and_the_split_under_exercises() -> None:
    """AC: "Deadlift 125 kg, push pull legs" puts the lift under level and the split under
    exercises."""
    for each in _athlete():
        await _at_goals(each, "gym")
        await _read(each, LEVEL, LEVEL_READ, "gym")

        event = _summary(each)

        assert _value(event, "gym.level") == "Deadlift 125 kg"
        assert _value(event, "gym.exercises") == "Push pull legs"


async def test_skipping_all_three_steps_marks_the_sport_section_skipped(athlete: Athlete) -> None:
    """AC: skipping all three steps marks the sport section "Skipped"."""
    await _picked(athlete)
    revision = await athlete.answers.store_answer(athlete.user.id, ABOUT_YOU, None, "31")
    await athlete.answers.save_read(athlete.user.id, ABOUT_YOU, None, revision, {"age": 31})
    for step in (GOALS, LEVEL, SCHEDULE):
        await athlete.answers.skip_answer(athlete.user.id, step, "climbing")

    event = _summary(athlete)

    section = re.search(r'<section[^>]*aria-label="Climbing".*?</section>', event, re.S)
    assert section is not None
    assert "Skipped" in section.group(0)
    assert event.count("Skipped") == 1
    assert _value(event, "climbing.goal_text") in (None, "")


async def test_one_skipped_step_leaves_the_section_unmarked(athlete: Athlete) -> None:
    await _at_goals(athlete)
    await athlete.answers.skip_answer(athlete.user.id, GOALS, "climbing")
    await _read(athlete, SCHEDULE, SCHEDULE_READ)

    assert "Skipped" not in _summary(athlete).split('aria-label="Climbing"')[1]


# --- Confirm ------------------------------------------------------------------------------


async def _due_summary(athlete: Athlete) -> None:
    await _at_goals(athlete)
    for step in (GOALS, LEVEL, SCHEDULE):
        await athlete.answers.skip_answer(athlete.user.id, step, "climbing")


async def test_confirm_saves_the_sports_profile_and_goals(athlete: Athlete) -> None:
    await _due_summary(athlete)

    response = athlete.client.post(
        "/onboarding/confirm",
        data={
            "timezone": "Europe/Warsaw",
            "climbing.goal_text": ["Send my first 7c outdoors", ""],
            "climbing.goal_date": ["2027-06-30", ""],
            "climbing.level": "Projecting 7b",
            "climbing.exercises": "",
            "climbing.sessions_per_week": "4",
            "climbing.session_minutes": "120",
            "climbing.days": ["0", "2", "4", "5"],
        },
        headers=HX,
    )

    assert response.headers["HX-Redirect"] == "/"
    assert athlete.answers.sport_profiles[athlete.user.id] == {
        "climbing": SportProfile(
            level="Projecting 7b",
            sessions_per_week=4,
            session_minutes=120,
            preferred_days=(0, 2, 4, 5),
        )
    }
    assert athlete.answers.goals[athlete.user.id] == [
        Goal(text="Send my first 7c outdoors", target_date=date(2027, 6, 30), sport_id="climbing")
    ]


async def test_skipping_all_three_steps_saves_no_goals(athlete: Athlete) -> None:
    """AC: skipping all three steps saves no goals."""
    await _due_summary(athlete)

    response = athlete.client.post(
        "/onboarding/confirm",
        data={"timezone": "UTC", "climbing.goal_text": "", "climbing.goal_date": ""},
        headers=HX,
    )

    assert response.headers["HX-Redirect"] == "/"
    assert athlete.answers.goals[athlete.user.id] == []


async def test_twenty_sessions_a_week_are_rejected_inline(athlete: Athlete) -> None:
    """AC: 20 sessions a week entered on the summary is rejected inline."""
    await _due_summary(athlete)

    response = athlete.client.post(
        "/onboarding/confirm",
        data={
            "timezone": "UTC",
            "climbing.sessions_per_week": "20",
            "climbing.days": ["1"],
            "climbing.goal_text": "Boulder 7A",
            "climbing.goal_date": "",
        },
        headers=HX,
    )

    assert response.status_code == 422
    assert "<html" not in response.text
    field = re.search(r'<input[^>]*name="climbing\.sessions_per_week"[^>]*>', response.text)
    assert field is not None
    assert 'aria-invalid="true"' in field.group(0)
    assert 'value="20"' in field.group(0)
    assert "From 1 to 14 sessions a week." in response.text
    # What was typed elsewhere stays as typed.
    assert _value(response.text, "climbing.goal_text") == "Boulder 7A"
    assert _checked_days(response.text, "climbing") == ["1"]
    assert athlete.answers.goals == {}
    assert athlete.answers.onboarded_at == {}


async def test_a_date_without_its_goal_is_flagged_on_that_goal(athlete: Athlete) -> None:
    await _due_summary(athlete)

    response = athlete.client.post(
        "/onboarding/confirm",
        data={"timezone": "UTC", "climbing.goal_text": "", "climbing.goal_date": "2027-06-30"},
        headers=HX,
    )

    assert response.status_code == 422
    goal = re.search(r'<input[^>]*name="climbing\.goal_text"[^>]*>', response.text)
    assert goal is not None
    assert 'aria-invalid="true"' in goal.group(0)
    assert "Add the goal this date is for." in response.text
    assert _value(response.text, "climbing.goal_date") == "2027-06-30"
