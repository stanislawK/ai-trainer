"""Another sport before the summary (PRD 0008 B33, B35, F20, F21, ADR-0028, ticket #256): after a
sport's schedule, "Add another sport?" lists the sports not picked yet and "No, show my summary";
a later sport gets only goals, level and schedule; the summary has a section per sport and
confirm saves each one."""

import re
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from ai_trainer.application.ai_onboarding import (
    AlreadyPickedError,
    OnboardingCard,
    Place,
    add_sport,
    current_card,
    pick_sport,
)
from ai_trainer.domain.onboarding import OnboardingStep
from ai_trainer.domain.sport_profile import SportProfile
from ai_trainer.domain.sports.registry import UnknownSportError, default_sport_registry
from tests.onboarding_support import FakeOnboardingAnswers
from tests.unit.test_ai_onboarding_route import HX, Athlete, _athlete, _picked, _summary

ABOUT_YOU, GOALS, LEVEL, SCHEDULE = (
    OnboardingStep.ABOUT_YOU,
    OnboardingStep.GOALS,
    OnboardingStep.LEVEL,
    OnboardingStep.SCHEDULE,
)
SPORT_STEPS = (GOALS, LEVEL, SCHEDULE)


@pytest.fixture
def athlete() -> Iterator[Athlete]:
    yield from _athlete()


async def _done(answers: FakeOnboardingAnswers, user_id: UUID, sport: str) -> None:
    for step in SPORT_STEPS:
        await answers.skip_answer(user_id, step, sport)


async def _climbed(athlete: Athlete) -> None:
    """Climbing picked, About you and its three steps skipped: "Add another sport?" is due."""
    await _picked(athlete)
    await athlete.answers.skip_answer(athlete.user.id, ABOUT_YOU, None)
    await _done(athlete.answers, athlete.user.id, "climbing")


# --- The application layer ----------------------------------------------------------------


async def test_after_the_first_sports_schedule_another_sport_is_offered() -> None:
    answers, user = FakeOnboardingAnswers(), uuid4()
    registry = default_sport_registry()
    await answers.start(user, "climbing", "UTC")
    await answers.skip_answer(user, ABOUT_YOU, None)
    await _done(answers, user, "climbing")

    assert await current_card(user, answers=answers, registry=registry) == Place(
        OnboardingCard.MORE
    )


async def test_a_second_sport_gets_its_three_steps_and_no_about_you() -> None:
    """AC: gym picked after climbing gets three gym chats and no About you."""
    answers, user = FakeOnboardingAnswers(), uuid4()
    registry = default_sport_registry()
    await answers.start(user, "climbing", "UTC")
    await answers.skip_answer(user, ABOUT_YOU, None)
    await _done(answers, user, "climbing")

    await add_sport(user, "gym", registry=registry, answers=answers)

    assert await answers.list_sports(user) == ["climbing", "gym"]
    for step in SPORT_STEPS:
        place = await current_card(user, answers=answers, registry=registry)
        assert place == Place(OnboardingCard(step.value), "gym")
        await answers.skip_answer(user, step, "gym")
    # Climbing's answers are kept.
    assert len([a for a in await answers.list_answers(user) if a.sport_id == "climbing"]) == 3


async def test_with_every_sport_picked_the_summary_is_due_without_the_offer() -> None:
    """AC: after all three sports, there is no "Add another sport?" card."""
    answers, user = FakeOnboardingAnswers(), uuid4()
    registry = default_sport_registry()
    await answers.start(user, "climbing", "UTC")
    await answers.skip_answer(user, ABOUT_YOU, None)
    await _done(answers, user, "climbing")
    for sport in ("gym", "cycling"):
        await add_sport(user, sport, registry=registry, answers=answers)
        await _done(answers, user, sport)

    assert await current_card(user, answers=answers, registry=registry) == Place(
        OnboardingCard.SUMMARY
    )


async def test_a_sport_already_picked_or_unknown_is_rejected_and_stores_nothing() -> None:
    answers, user = FakeOnboardingAnswers(), uuid4()
    registry = default_sport_registry()
    await answers.start(user, "climbing", "UTC")

    with pytest.raises(AlreadyPickedError):
        await add_sport(user, "climbing", registry=registry, answers=answers)
    with pytest.raises(UnknownSportError):
        await add_sport(user, "chess", registry=registry, answers=answers)

    assert await answers.list_sports(user) == ["climbing"]


async def test_picking_the_first_sport_again_keeps_the_sport_added_since() -> None:
    """Back from About you reaches the sport card; tapping the same sport there to go on must not
    drop the gym or its answers."""
    answers, user = FakeOnboardingAnswers(), uuid4()
    registry = default_sport_registry()
    await pick_sport(user, "climbing", "Europe/Warsaw", registry=registry, answers=answers)
    await add_sport(user, "gym", registry=registry, answers=answers)
    await answers.skip_answer(user, GOALS, "gym")

    await pick_sport(user, "climbing", "UTC", registry=registry, answers=answers)

    assert await answers.list_sports(user) == ["climbing", "gym"]
    assert [a.sport_id for a in await answers.list_answers(user)] == ["gym"]
    assert answers.timezones[user] == "Europe/Warsaw"


# --- The routes ---------------------------------------------------------------------------


async def test_the_first_sports_schedule_leads_to_the_offer(athlete: Athlete) -> None:
    await _picked(athlete)
    await athlete.answers.skip_answer(athlete.user.id, ABOUT_YOU, None)
    for step in (GOALS, LEVEL):
        await athlete.answers.skip_answer(athlete.user.id, step, "climbing")

    response = athlete.client.post("/onboarding/climbing/schedule/skip", headers=HX)

    assert response.headers["HX-Redirect"] == "/onboarding/more"


async def test_the_offer_lists_only_the_sports_not_picked_and_the_way_to_the_summary(
    athlete: Athlete,
) -> None:
    await _climbed(athlete)

    response = athlete.client.get("/onboarding/more")

    assert response.status_code == 200
    assert "<html" in response.text
    assert "Add another sport?" in response.text
    assert "Your personal details carry over." in response.text
    assert '"sport": "climbing"' not in response.text
    for sport in ("gym", "cycling"):
        assert f'hx-vals=\'{{"sport": "{sport}"}}\'' in response.text
    assert 'href="/onboarding/summary"' in response.text
    assert "No, show my summary" in response.text
    # Back to the last sport's schedule; the next round's progress starts here, 1 of 4.
    assert 'href="/onboarding/climbing/schedule"' in response.text
    assert 'aria-valuenow="1"' in response.text
    assert 'aria-valuemax="4"' in response.text


async def test_the_offer_with_hx_request_is_a_partial(athlete: Athlete) -> None:
    await _climbed(athlete)

    response = athlete.client.get("/onboarding/more", headers=HX)

    assert "<html" not in response.text
    assert "Add another sport?" in response.text


async def test_gym_picked_after_climbing_gets_three_gym_chats_and_no_about_you(
    athlete: Athlete,
) -> None:
    """AC: gym picked after climbing gets three gym chats and no About you."""
    await _climbed(athlete)

    picked = athlete.client.post("/onboarding/more", data={"sport": "gym"}, headers=HX)
    assert picked.headers["HX-Redirect"] == "/onboarding/gym/goals"

    goals = athlete.client.get("/onboarding/gym/goals")
    assert "What are you training for in the gym?" in goals.text
    # Back skips the offer: the sport is already stored, so it goes to climbing's schedule.
    assert 'href="/onboarding/climbing/schedule"' in goals.text
    assert 'aria-valuenow="2"' in goals.text
    assert 'aria-valuemax="4"' in goals.text

    sent = athlete.client.post(
        "/onboarding/gym/goals", data={"answer": "Deadlift 150 kg"}, headers=HX
    )
    level = athlete.client.post("/onboarding/gym/level/skip", headers=HX)
    schedule = athlete.client.post("/onboarding/gym/schedule/skip", headers=HX)

    assert sent.headers["HX-Redirect"] == "/onboarding/gym/level"
    assert level.headers["HX-Redirect"] == "/onboarding/gym/schedule"
    assert schedule.headers["HX-Redirect"] == "/onboarding/more"
    gym = {
        a.step for a in await athlete.answers.list_answers(athlete.user.id) if a.sport_id == "gym"
    }
    assert gym == set(SPORT_STEPS)
    # About you was answered once, for the athlete, never for the gym.
    assert [
        a.sport_id
        for a in await athlete.answers.list_answers(athlete.user.id)
        if a.step is ABOUT_YOU
    ] == [None]


async def test_after_all_three_sports_there_is_no_offer(athlete: Athlete) -> None:
    """AC: after all three sports, there is no "Add another sport?" card."""
    await _climbed(athlete)
    athlete.client.post("/onboarding/more", data={"sport": "gym"}, headers=HX)
    await _done(athlete.answers, athlete.user.id, "gym")
    athlete.client.post("/onboarding/more", data={"sport": "cycling"}, headers=HX)
    for step in (GOALS, LEVEL):
        await athlete.answers.skip_answer(athlete.user.id, step, "cycling")

    last = athlete.client.post("/onboarding/cycling/schedule/skip", headers=HX)
    offer = athlete.client.get("/onboarding/more", follow_redirects=False)
    start = athlete.client.get("/onboarding", follow_redirects=False)

    assert last.headers["HX-Redirect"] == "/onboarding/summary"
    assert offer.headers["location"] == "/onboarding/summary"
    assert start.headers["location"] == "/onboarding/summary"


async def test_the_offer_resumes_on_a_reload_and_the_summary_stays_open(athlete: Athlete) -> None:
    """ "No, show my summary" stores nothing: the summary opens while the offer is due, and a
    reload of `/onboarding` comes back to the offer."""
    await _climbed(athlete)

    start = athlete.client.get("/onboarding", follow_redirects=False)
    summary = athlete.client.get("/onboarding/summary", follow_redirects=False)

    assert start.headers["location"] == "/onboarding/more"
    assert summary.status_code == 200


async def test_the_offer_before_the_first_sport_is_done_sends_the_athlete_on(
    athlete: Athlete,
) -> None:
    await _picked(athlete)
    await athlete.answers.skip_answer(athlete.user.id, ABOUT_YOU, None)

    shown = athlete.client.get("/onboarding/more", follow_redirects=False)
    picked = athlete.client.post("/onboarding/more", data={"sport": "gym"}, follow_redirects=False)

    assert shown.headers["location"] == "/onboarding/climbing/goals"
    assert picked.headers["location"] == "/onboarding/climbing/goals"
    assert await athlete.answers.list_sports(athlete.user.id) == ["climbing"]


async def test_a_picked_or_unknown_sport_on_the_offer_is_rejected(athlete: Athlete) -> None:
    await _climbed(athlete)

    for sport in ("climbing", "chess", ""):
        response = athlete.client.post("/onboarding/more", data={"sport": sport}, headers=HX)
        assert response.status_code == 422, sport
    assert await athlete.answers.list_sports(athlete.user.id) == ["climbing"]


async def test_a_sport_not_picked_has_no_cards(athlete: Athlete) -> None:
    await _climbed(athlete)

    for path in ("/onboarding/gym/goals", "/onboarding/chess/level"):
        response = athlete.client.get(path, follow_redirects=False)
        assert response.headers["location"] == "/onboarding/more", path
    response = athlete.client.post("/onboarding/gym/goals/skip", follow_redirects=False)
    assert response.headers["location"] == "/onboarding/more"
    assert not [
        a for a in await athlete.answers.list_answers(athlete.user.id) if a.sport_id == "gym"
    ]


async def test_the_summary_has_about_you_and_a_section_per_sport(athlete: Athlete) -> None:
    """AC: the summary has About you plus two sport sections."""
    await _climbed(athlete)
    athlete.client.post("/onboarding/more", data={"sport": "gym"}, headers=HX)
    await _done(athlete.answers, athlete.user.id, "gym")

    skeleton = athlete.client.get("/onboarding/summary")
    event = _summary(athlete)

    for label in ("Climbing", "Gym"):
        assert f">{label}</div>" in skeleton.text
        assert re.search(rf'<section[^>]*aria-label="{label}"', event), label
    assert re.search(r'<section[^>]*aria-label="About you"', event)
    assert 'name="gym.sessions_per_week"' in event


async def test_confirm_saves_both_sports(athlete: Athlete) -> None:
    """AC: confirm saves both sport sections."""
    await _climbed(athlete)
    athlete.client.post("/onboarding/more", data={"sport": "gym"}, headers=HX)
    await _done(athlete.answers, athlete.user.id, "gym")

    response = athlete.client.post(
        "/onboarding/confirm",
        data={
            "timezone": "Europe/Warsaw",
            "climbing.level": "Projecting 7b",
            "climbing.goal_text": "",
            "climbing.goal_date": "",
            "gym.sessions_per_week": "2",
            "gym.session_minutes": "60",
            "gym.goal_text": "Deadlift 150 kg",
            "gym.goal_date": "",
        },
        headers=HX,
    )

    assert response.headers["HX-Redirect"] == "/"
    assert athlete.answers.sports[athlete.user.id] == ["climbing", "gym"]
    assert athlete.answers.sport_profiles[athlete.user.id] == {
        "climbing": SportProfile(level="Projecting 7b"),
        "gym": SportProfile(sessions_per_week=2, session_minutes=60),
    }
    assert [(g.text, g.sport_id) for g in athlete.answers.goals[athlete.user.id]] == [
        ("Deadlift 150 kg", "gym")
    ]


def test_the_old_form_steps_are_gone() -> None:
    """AC: `/onboarding/availability` returns 404, and so does the old timezone step."""
    for athlete in _athlete():
        client: TestClient = athlete.client
        for path in ("/onboarding/availability", "/onboarding/timezone"):
            assert client.get(path).status_code == 404, path
            assert client.post(path, data={}).status_code in (404, 405), path
