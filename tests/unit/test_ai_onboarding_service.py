"""The AI onboarding's application layer (PRD-0007 B34-B36, ADR-0028, ticket #254): a send stores
the answer and returns before its read, a stale read never wins, the summary catches up on
reads a restart lost, and confirm writes the profile and clears the answers."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from ai_trainer.application.ai_onboarding import (
    EmptyAnswerError,
    InvalidSummaryError,
    OnboardingCard,
    catch_up,
    confirm_summary,
    current_card,
    pick_sport,
    send_answer,
    skip_step,
    summary_of,
)
from ai_trainer.application.background_reads import BackgroundReads
from ai_trainer.domain.athlete_profile import AboutYou, AthleteProfile, FieldProblem
from ai_trainer.domain.onboarding import AnswerStatus, OnboardingStep
from ai_trainer.domain.sports.registry import UnknownSportError, default_sport_registry
from tests.onboarding_support import FakeOnboardingAnswers, FakeReader

ABOUT_YOU = OnboardingStep.ABOUT_YOU
NOW = datetime(2026, 10, 24, 18, 0, tzinfo=UTC)
READ: dict[str, JsonValue] = {
    "age": 31,
    "height_cm": 178,
    "weight_kg": 73.0,
    "limits": "Sore left shoulder",
}


class FakeClock:
    def now(self) -> datetime:
        return NOW


@pytest.fixture
def answers() -> FakeOnboardingAnswers:
    return FakeOnboardingAnswers()


@pytest.fixture
async def reads() -> BackgroundReads:
    return BackgroundReads()


async def _send(
    user: UUID,
    text: str,
    answers: FakeOnboardingAnswers,
    reads: BackgroundReads,
    reader: FakeReader,
) -> None:
    await send_answer(
        user,
        ABOUT_YOU,
        text,
        answers=answers,
        reads=reads,
        reader=reader,
        sport_id="climbing",
        timezone="Europe/Warsaw",
    )


async def test_the_pick_stores_the_sport_and_the_browsers_zone(
    answers: FakeOnboardingAnswers,
) -> None:
    user = uuid4()

    await pick_sport(
        user, "climbing", "Europe/Warsaw", registry=default_sport_registry(), answers=answers
    )

    assert answers.sports[user] == ["climbing"]
    assert answers.timezones[user] == "Europe/Warsaw"


async def test_a_zone_the_browser_cannot_name_falls_back_to_utc(
    answers: FakeOnboardingAnswers,
) -> None:
    user = uuid4()

    await pick_sport(
        user, "gym", "Mars/Olympus", registry=default_sport_registry(), answers=answers
    )

    assert answers.timezones[user] == "UTC"


async def test_an_unknown_sport_is_rejected_and_stores_nothing(
    answers: FakeOnboardingAnswers,
) -> None:
    with pytest.raises(UnknownSportError):
        await pick_sport(
            uuid4(), "chess", "UTC", registry=default_sport_registry(), answers=answers
        )
    assert answers.sports == {}


async def test_the_card_follows_the_sport_then_about_you_then_the_sports_steps(
    answers: FakeOnboardingAnswers,
) -> None:
    user = uuid4()
    assert await current_card(user, answers=answers) is OnboardingCard.SPORT
    await answers.start(user, "climbing", "UTC")
    assert await current_card(user, answers=answers) is OnboardingCard.ABOUT_YOU
    await answers.skip_answer(user, ABOUT_YOU, None)
    # Then goals, level and schedule (#255), and then the summary.
    assert await current_card(user, answers=answers) is OnboardingCard.GOALS


async def test_a_send_returns_before_the_model_call_does(
    answers: FakeOnboardingAnswers, reads: BackgroundReads
) -> None:
    """AC: the next step appears before the model call returns."""
    user = uuid4()
    reader = FakeReader(READ)
    reader.hold = True

    await _send(user, "31, 1,78 m, 73 kg, sore left shoulder", answers, reads, reader)

    [answer] = await answers.list_answers(user)
    assert answer.status is AnswerStatus.PENDING
    await asyncio.wait_for(reader.started.wait(), 1)
    reader.release.set()
    await reads.drain()
    [answer] = await answers.list_answers(user)
    assert (answer.status, answer.parsed) == (AnswerStatus.PARSED, READ)


async def test_a_blank_answer_is_rejected_and_starts_no_read(
    answers: FakeOnboardingAnswers, reads: BackgroundReads
) -> None:
    reader = FakeReader(READ)
    with pytest.raises(EmptyAnswerError):
        await _send(uuid4(), "  \x00 ", answers, reads, reader)
    await reads.drain()
    assert reader.calls == []
    assert answers.answers == {}


async def test_an_answer_longer_than_the_column_is_cut_to_it(
    answers: FakeOnboardingAnswers, reads: BackgroundReads
) -> None:
    user = uuid4()
    await _send(user, "a" * 1200, answers, reads, FakeReader(READ))
    await reads.drain()
    [answer] = await answers.list_answers(user)
    assert answer.text == "a" * 1000


async def test_a_failed_read_marks_the_answer_failed(
    answers: FakeOnboardingAnswers, reads: BackgroundReads
) -> None:
    user = uuid4()
    await _send(user, "31", answers, reads, FakeReader(None))
    await reads.drain()
    [answer] = await answers.list_answers(user)
    assert answer.status is AnswerStatus.FAILED


async def test_a_read_that_raises_marks_the_answer_failed_and_is_dropped(
    answers: FakeOnboardingAnswers, reads: BackgroundReads
) -> None:
    def boom(text: str) -> dict[str, JsonValue]:
        raise RuntimeError("upstream exploded")

    user = uuid4()
    await _send(user, "31", answers, reads, FakeReader(boom))
    await reads.drain()
    [answer] = await answers.list_answers(user)
    assert answer.status is AnswerStatus.FAILED
    assert reads.running((user, ABOUT_YOU, None)) is None


async def test_after_back_and_a_new_answer_the_older_read_never_wins(
    answers: FakeOnboardingAnswers, reads: BackgroundReads
) -> None:
    """AC: after Back and a new answer, the older read never wins."""
    user = uuid4()
    slow = FakeReader({"age": 31})
    slow.hold = True
    await _send(user, "I'm 31", answers, reads, slow)
    await asyncio.wait_for(slow.started.wait(), 1)

    await _send(user, "Actually 32", answers, reads, FakeReader({"age": 32}))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    slow.release.set()
    await reads.drain()

    [answer] = await answers.list_answers(user)
    assert (answer.text, answer.parsed) == ("Actually 32", {"age": 32})


async def test_a_skip_stores_skipped_and_starts_no_read(
    answers: FakeOnboardingAnswers, reads: BackgroundReads
) -> None:
    user = uuid4()
    await skip_step(user, ABOUT_YOU, answers=answers)
    [answer] = await answers.list_answers(user)
    assert (answer.status, answer.text) == (AnswerStatus.SKIPPED, None)
    assert reads.running((user, ABOUT_YOU, None)) is None


async def test_the_summary_awaits_a_read_still_in_flight(
    answers: FakeOnboardingAnswers, reads: BackgroundReads
) -> None:
    user = uuid4()
    await answers.start(user, "climbing", "Europe/Warsaw")
    reader = FakeReader(READ)
    reader.hold = True
    await _send(user, "31, 5 ft 10", answers, reads, reader)
    await asyncio.wait_for(reader.started.wait(), 1)

    catching_up = asyncio.create_task(
        catch_up(user, answers=answers, reads=reads, reader=reader, timezone="Europe/Warsaw")
    )
    await asyncio.sleep(0)
    assert not catching_up.done()
    reader.release.set()
    await catching_up

    assert len(reader.calls) == 1
    [answer] = await answers.list_answers(user)
    assert answer.status is AnswerStatus.PARSED


async def test_a_restart_mid_read_still_fills_the_summary(
    answers: FakeOnboardingAnswers,
) -> None:
    """AC: a restart mid-read still fills the summary. The row is pending and this process
    holds no task for it, so the summary reads it once itself."""
    user = uuid4()
    await answers.start(user, "climbing", "Europe/Warsaw")
    await answers.store_answer(user, ABOUT_YOU, None, "31, 5 ft 10, 160 lb")
    reader = FakeReader(READ)

    await catch_up(
        user, answers=answers, reads=BackgroundReads(), reader=reader, timezone="Europe/Warsaw"
    )

    assert reader.calls == [(user, "31, 5 ft 10, 160 lb", "climbing", "Europe/Warsaw")]
    summary = await summary_of(user, answers=answers, timezone="Europe/Warsaw")
    assert summary.about_you == AboutYou(
        age=31, height_cm=178, weight_kg=73, limits="Sore left shoulder"
    )


async def test_a_read_that_fails_twice_leaves_the_fields_empty_and_the_summary_opens(
    answers: FakeOnboardingAnswers, reads: BackgroundReads
) -> None:
    user = uuid4()
    await answers.start(user, "climbing", "UTC")
    reader = FakeReader(None)
    await _send(user, "31", answers, reads, reader)
    await reads.drain()

    await catch_up(user, answers=answers, reads=reads, reader=reader, timezone="UTC")

    assert len(reader.calls) == 2
    summary = await summary_of(user, answers=answers, timezone="UTC")
    assert summary.about_you == AboutYou()
    assert summary.about_you_skipped is False


async def test_the_summary_reads_nothing_already_parsed_or_skipped(
    answers: FakeOnboardingAnswers,
) -> None:
    user = uuid4()
    await answers.start(user, "climbing", "UTC")
    await answers.skip_answer(user, ABOUT_YOU, None)
    reader = FakeReader(READ)

    await catch_up(user, answers=answers, reads=BackgroundReads(), reader=reader, timezone="UTC")

    assert reader.calls == []
    summary = await summary_of(user, answers=answers, timezone="UTC")
    assert summary.about_you_skipped is True
    assert summary.about_you == AboutYou()


async def test_a_read_with_a_value_out_of_bounds_shows_it_empty(
    answers: FakeOnboardingAnswers,
) -> None:
    user = uuid4()
    revision = await answers.store_answer(user, ABOUT_YOU, None, "I'm 7 and 400 kg")
    await answers.save_read(user, ABOUT_YOU, None, revision, {"age": 7, "weight_kg": None})

    summary = await summary_of(user, answers=answers, timezone="UTC")

    assert summary.about_you == AboutYou()


async def test_the_summary_shows_the_stored_zone(answers: FakeOnboardingAnswers) -> None:
    summary = await summary_of(uuid4(), answers=answers, timezone="Europe/Warsaw")
    assert summary.timezone == "Europe/Warsaw"


async def test_confirm_saves_the_corrected_fields_and_leaves_no_answers(
    answers: FakeOnboardingAnswers,
) -> None:
    """AC: a corrected field is saved; no answers remain after confirm."""
    user = uuid4()
    await answers.start(user, "climbing", "UTC")
    revision = await answers.store_answer(user, ABOUT_YOU, None, "31, 160 lb")
    await answers.save_read(user, ABOUT_YOU, None, revision, READ)

    await confirm_summary(
        user,
        {
            "age": "31",
            "height_cm": "178",
            "weight_kg": "75",
            "limits": "Sore left shoulder",
            "timezone": "Europe/Warsaw",
        },
        answers=answers,
        clock=FakeClock(),
    )

    assert answers.profiles[user] == AthleteProfile(
        birth_year=1995, height_cm=178, weight_kg=75, limits="Sore left shoulder"
    )
    assert answers.sports[user] == ["climbing"]
    assert answers.timezones[user] == "Europe/Warsaw"
    assert answers.onboarded_at[user] == NOW
    assert await answers.list_answers(user) == []


async def test_confirm_takes_the_birth_year_from_the_athletes_own_new_year() -> None:
    """At 23:30 UTC on 31 December it is already the next year in Warsaw."""
    answers = FakeOnboardingAnswers()
    user = uuid4()

    class NewYearsEve:
        def now(self) -> datetime:
            return datetime(2026, 12, 31, 23, 30, tzinfo=UTC)

    await confirm_summary(
        user, {"age": "31", "timezone": "Europe/Warsaw"}, answers=answers, clock=NewYearsEve()
    )

    assert answers.profiles[user].birth_year == 1996


async def test_confirm_with_a_bad_field_names_it_and_writes_nothing(
    answers: FakeOnboardingAnswers,
) -> None:
    user = uuid4()
    await answers.store_answer(user, ABOUT_YOU, None, "31")

    with pytest.raises(InvalidSummaryError) as raised:
        await confirm_summary(
            user,
            {"height_cm": "20", "timezone": "Not/AZone"},
            answers=answers,
            clock=FakeClock(),
        )

    assert raised.value.problems == {
        "height_cm": FieldProblem.OUT_OF_RANGE,
        "timezone": FieldProblem.UNKNOWN,
    }
    assert answers.profiles == {}
    assert len(await answers.list_answers(user)) == 1


async def test_one_athletes_answers_never_reach_anothers_summary(
    answers: FakeOnboardingAnswers,
) -> None:
    """AC: another athlete's answers are never shown."""
    first, second = uuid4(), uuid4()
    revision = await answers.store_answer(first, ABOUT_YOU, None, "31")
    await answers.save_read(first, ABOUT_YOU, None, revision, READ)
    reader = FakeReader(READ)

    await catch_up(second, answers=answers, reads=BackgroundReads(), reader=reader, timezone="UTC")

    assert reader.calls == []
    summary = await summary_of(second, answers=answers, timezone="UTC")
    assert summary.about_you == AboutYou()


async def test_closing_the_reads_cancels_any_still_running(
    answers: FakeOnboardingAnswers, reads: BackgroundReads
) -> None:
    user = uuid4()
    reader = FakeReader(READ)
    reader.hold = True
    await _send(user, "31", answers, reads, reader)
    await asyncio.wait_for(reader.started.wait(), 1)

    await reads.close()

    assert reads.running((user, ABOUT_YOU, None)) is None
    [answer] = await answers.list_answers(user)
    assert answer.status is AnswerStatus.PENDING
