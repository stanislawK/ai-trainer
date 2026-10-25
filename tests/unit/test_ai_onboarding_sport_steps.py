"""Goals, level and schedule for the chosen sport (PRD 0008 B33-B35, F20, F21, ADR-0028, ticket
#255): each step's answer is stored under the sport and read in the background with the sport,
the summary shows a section per sport, skipped when all three steps were, and confirm writes the
sport's profile and goals together with the rest."""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from ai_trainer.application.ai_onboarding import (
    InvalidSummaryError,
    OnboardingCard,
    Place,
    SportSummary,
    catch_up,
    confirm_summary,
    current_card,
    send_answer,
    skip_step,
    summary_of,
)
from ai_trainer.application.background_reads import BackgroundReads
from ai_trainer.domain.athlete_profile import FieldProblem
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.onboarding import AnswerStatus, OnboardingStep
from ai_trainer.domain.sport_profile import SportForm, SportProfile, SportSection
from ai_trainer.domain.sports.registry import default_sport_registry
from tests.onboarding_support import FakeOnboardingAnswers, FakeReader

ABOUT_YOU, GOALS, LEVEL, SCHEDULE = (
    OnboardingStep.ABOUT_YOU,
    OnboardingStep.GOALS,
    OnboardingStep.LEVEL,
    OnboardingStep.SCHEDULE,
)
NOW = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)
REGISTRY = default_sport_registry()
GOALS_READ: dict[str, JsonValue] = {
    "goals": [{"text": "Send my first 7c outdoors", "target_date": "2027-06-30"}]
}
SCHEDULE_READ: dict[str, JsonValue] = {
    "sessions_per_week": 4,
    "session_minutes": 120,
    "preferred_days": [0, 2, 4, 5],
}


class FakeClock:
    def now(self) -> datetime:
        return NOW


@pytest.fixture
def answers() -> FakeOnboardingAnswers:
    return FakeOnboardingAnswers()


async def _climber(answers: FakeOnboardingAnswers) -> UUID:
    user = uuid4()
    await answers.start(user, "climbing", "Europe/Warsaw")
    await answers.skip_answer(user, ABOUT_YOU, None)
    return user


async def test_the_card_follows_about_you_with_goals_level_and_schedule(
    answers: FakeOnboardingAnswers,
) -> None:
    user = await _climber(answers)

    for step, card in (
        (GOALS, OnboardingCard.GOALS),
        (LEVEL, OnboardingCard.LEVEL),
        (SCHEDULE, OnboardingCard.SCHEDULE),
    ):
        assert await current_card(user, answers=answers, registry=REGISTRY) == Place(
            card, "climbing"
        )
        await answers.skip_answer(user, step, "climbing")

    # The other two sports are left to add (#256).
    assert await current_card(user, answers=answers, registry=REGISTRY) == Place(
        OnboardingCard.MORE
    )


async def test_a_sport_step_is_stored_under_the_sport_and_read_with_it(
    answers: FakeOnboardingAnswers,
) -> None:
    user = await _climber(answers)
    reads = BackgroundReads()
    reader = FakeReader(GOALS_READ)

    await send_answer(
        user,
        GOALS,
        "Send my first 7c outdoors by June",
        answers=answers,
        reads=reads,
        reader=reader,
        sport_id="climbing",
        timezone="Europe/Warsaw",
    )
    assert reads.running((user, GOALS, "climbing")) is not None
    await reads.drain()

    [_, answer] = await answers.list_answers(user)
    assert (answer.step, answer.sport_id, answer.status) == (GOALS, "climbing", AnswerStatus.PARSED)
    assert answer.parsed == GOALS_READ
    assert reader.calls == [
        (user, "Send my first 7c outdoors by June", "climbing", "Europe/Warsaw")
    ]


async def test_a_skipped_sport_step_is_stored_under_the_sport(
    answers: FakeOnboardingAnswers,
) -> None:
    user = await _climber(answers)

    await skip_step(user, SCHEDULE, answers=answers, sport_id="climbing")

    [_, answer] = await answers.list_answers(user)
    assert (answer.step, answer.sport_id, answer.status) == (
        SCHEDULE,
        "climbing",
        AnswerStatus.SKIPPED,
    )


async def test_the_summary_catches_up_on_a_sport_step_with_its_own_sport(
    answers: FakeOnboardingAnswers,
) -> None:
    user = await _climber(answers)
    await answers.store_answer(user, SCHEDULE, "climbing", "4 times a week")
    reader = FakeReader(SCHEDULE_READ)

    await catch_up(user, answers=answers, reads=BackgroundReads(), reader=reader, timezone="UTC")

    assert [(call[2], call[3]) for call in reader.calls] == [("climbing", "UTC")]
    summary = await summary_of(user, answers=answers, timezone="UTC")
    assert summary.sports[0].section.profile.sessions_per_week == 4


async def test_the_summary_has_a_section_for_the_sport_with_what_was_read(
    answers: FakeOnboardingAnswers,
) -> None:
    user = await _climber(answers)
    for step, read in ((GOALS, GOALS_READ), (SCHEDULE, SCHEDULE_READ)):
        revision = await answers.store_answer(user, step, "climbing", "x")
        await answers.save_read(user, step, "climbing", revision, read)
    await answers.skip_answer(user, LEVEL, "climbing")

    summary = await summary_of(user, answers=answers, timezone="Europe/Warsaw")

    assert summary.sports == (
        SportSummary(
            sport_id="climbing",
            section=SportSection(
                goals=(
                    Goal(
                        text="Send my first 7c outdoors",
                        target_date=date(2027, 6, 30),
                        sport_id="climbing",
                    ),
                ),
                profile=SportProfile(
                    sessions_per_week=4, session_minutes=120, preferred_days=(0, 2, 4, 5)
                ),
            ),
            skipped=False,
        ),
    )


async def test_skipping_all_three_steps_marks_the_sport_skipped(
    answers: FakeOnboardingAnswers,
) -> None:
    """AC: skipping all three steps marks the sport section "Skipped"."""
    user = await _climber(answers)
    for step in (GOALS, LEVEL, SCHEDULE):
        await answers.skip_answer(user, step, "climbing")

    summary = await summary_of(user, answers=answers, timezone="UTC")

    assert summary.sports == (
        SportSummary(sport_id="climbing", section=SportSection(), skipped=True),
    )


def _form(**fields: str) -> SportForm:
    return SportForm(fields=fields)


async def test_confirm_writes_the_sports_profile_and_its_goals(
    answers: FakeOnboardingAnswers,
) -> None:
    user = await _climber(answers)

    await confirm_summary(
        user,
        {"timezone": "Europe/Warsaw"},
        sport_forms={
            "climbing": SportForm(
                fields={"level": "7b", "sessions_per_week": "4", "session_minutes": "120"},
                days=("0", "2", "4", "5"),
                goal_texts=("Send my first 7c outdoors",),
                goal_dates=("2027-06-30",),
            )
        },
        answers=answers,
        clock=FakeClock(),
    )

    assert answers.sport_profiles[user] == {
        "climbing": SportProfile(
            level="7b", sessions_per_week=4, session_minutes=120, preferred_days=(0, 2, 4, 5)
        )
    }
    assert answers.goals[user] == [
        Goal(text="Send my first 7c outdoors", target_date=date(2027, 6, 30), sport_id="climbing")
    ]
    assert await answers.list_answers(user) == []


async def test_skipping_all_three_steps_saves_no_goals(answers: FakeOnboardingAnswers) -> None:
    """AC: skipping all three steps saves no goals."""
    user = await _climber(answers)
    for step in (GOALS, LEVEL, SCHEDULE):
        await answers.skip_answer(user, step, "climbing")

    await confirm_summary(
        user,
        {"timezone": "UTC"},
        sport_forms={"climbing": _form()},
        answers=answers,
        clock=FakeClock(),
    )

    assert answers.goals[user] == []
    assert answers.sport_profiles[user] == {"climbing": SportProfile()}


async def test_twenty_sessions_a_week_are_rejected_under_the_sport_and_nothing_is_written(
    answers: FakeOnboardingAnswers,
) -> None:
    """AC: 20 sessions a week entered on the summary is rejected inline."""
    user = await _climber(answers)

    with pytest.raises(InvalidSummaryError) as raised:
        await confirm_summary(
            user,
            {"timezone": "UTC", "age": "5"},
            sport_forms={"climbing": _form(sessions_per_week="20")},
            answers=answers,
            clock=FakeClock(),
        )

    assert raised.value.problems == {
        "age": FieldProblem.OUT_OF_RANGE,
        "climbing.sessions_per_week": FieldProblem.OUT_OF_RANGE,
    }
    assert answers.goals == {}
    assert answers.onboarded_at == {}


async def test_confirm_takes_only_the_athletes_own_sports(answers: FakeOnboardingAnswers) -> None:
    """A form for a sport the athlete did not pick is ignored, and a missing one is blank."""
    user = await _climber(answers)

    await confirm_summary(
        user,
        {"timezone": "UTC"},
        sport_forms={"gym": SportForm(goal_texts=("Deadlift 150 kg",))},
        answers=answers,
        clock=FakeClock(),
    )

    assert answers.sport_profiles[user] == {"climbing": SportProfile()}
    assert answers.goals[user] == []
