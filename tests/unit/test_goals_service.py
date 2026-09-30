"""`set_goals` stores the athlete's goals: free text of 1-200 characters and an optional target
date that is not in the past, with at least one goal (PRD-0003 F6, B9, ADR-0006, ADR-0014,
ticket #76)."""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.onboarding import (
    MAX_GOAL_LENGTH,
    GoalProblem,
    InvalidGoalsError,
    NoGoalsError,
    UnknownGoalSportError,
    earliest_today,
    set_goals,
)
from ai_trainer.domain.goals import Goal


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


# 12:00 UTC on 2026-09-29: it is already 30 September east of UTC+12 and still 29 September
# at UTC-12, the last place on Earth to reach each date.
NOON_UTC = FakeClock(datetime(2026, 9, 29, 12, 0, tzinfo=UTC))


class FakeOnboardingRepository:
    def __init__(self) -> None:
        self.goals: dict[UUID, list[Goal]] = {}
        self.sports: dict[UUID, list[str]] = {}
        self.replace_calls = 0

    async def replace_sports(
        self,
        user_id: UUID,
        sport_ids: Sequence[str],
        *,
        general_goals_of: Sequence[str] = (),
        delete_goals_of: Sequence[str] = (),
    ) -> None:
        raise NotImplementedError

    async def list_sports(self, user_id: UUID) -> Sequence[str]:
        return self.sports.get(user_id, [])

    async def replace_availability(
        self, user_id: UUID, minutes_by_weekday: Mapping[int, int]
    ) -> None:
        raise NotImplementedError

    async def list_availability(self, user_id: UUID) -> Mapping[int, int]:
        raise NotImplementedError

    async def replace_goals(self, user_id: UUID, goals: Sequence[Goal]) -> None:
        self.replace_calls += 1
        self.goals[user_id] = list(goals)

    async def list_goals(self, user_id: UUID) -> Sequence[Goal]:
        return self.goals.get(user_id, [])

    async def set_onboarded_at(self, user_id: UUID, when: datetime | None) -> None:
        raise NotImplementedError

    async def finish_onboarding(self, user_id: UUID, timezone: str, when: datetime) -> None:
        raise NotImplementedError


async def test_one_goal_with_a_date_and_one_without_are_both_stored_in_order() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()

    await set_goals(
        user_id,
        [(None, "Send 8a+ by spring", "2027-04-30"), (None, "Ride 100 km in one go", "")],
        clock=NOON_UTC,
        repository=repository,
    )

    assert repository.goals == {
        user_id: [
            Goal(text="Send 8a+ by spring", target_date=date(2027, 4, 30)),
            Goal(text="Ride 100 km in one go", target_date=None),
        ]
    }


async def test_goal_text_is_trimmed() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()

    await set_goals(
        user_id, [(None, "  Stay consistent \n", " ")], clock=NOON_UTC, repository=repository
    )

    assert repository.goals[user_id] == [Goal(text="Stay consistent", target_date=None)]


async def test_no_goals_raises_and_stores_nothing() -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(NoGoalsError):
        await set_goals(uuid4(), [], clock=NOON_UTC, repository=repository)

    assert repository.replace_calls == 0


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
async def test_a_blank_goal_is_flagged_and_nothing_is_stored(blank: str) -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(InvalidGoalsError) as raised:
        await set_goals(
            uuid4(),
            [(None, "Climb 7a", ""), (None, blank, "2027-01-01")],
            clock=NOON_UTC,
            repository=repository,
        )

    assert raised.value.problems == {1: GoalProblem.BLANK}
    assert repository.replace_calls == 0


async def test_a_blank_goal_with_a_date_is_still_blank() -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(InvalidGoalsError) as raised:
        await set_goals(uuid4(), [(None, "", "2027-01-01")], clock=NOON_UTC, repository=repository)

    assert raised.value.problems == {0: GoalProblem.BLANK}


async def test_200_characters_is_accepted_and_201_is_flagged() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    longest = "a" * MAX_GOAL_LENGTH

    await set_goals(user_id, [(None, longest, "")], clock=NOON_UTC, repository=repository)
    with pytest.raises(InvalidGoalsError) as raised:
        await set_goals(user_id, [(None, longest + "a", "")], clock=NOON_UTC, repository=repository)

    assert MAX_GOAL_LENGTH == 200
    assert repository.goals[user_id] == [Goal(text=longest, target_date=None)]
    assert raised.value.problems == {0: GoalProblem.TOO_LONG}
    assert repository.replace_calls == 1


async def test_length_is_judged_after_trimming() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()

    await set_goals(
        user_id,
        [(None, "  " + "a" * MAX_GOAL_LENGTH + "  ", "")],
        clock=NOON_UTC,
        repository=repository,
    )

    assert repository.goals[user_id] == [Goal(text="a" * MAX_GOAL_LENGTH, target_date=None)]


async def test_a_past_target_date_is_flagged_and_nothing_is_stored() -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(InvalidGoalsError) as raised:
        await set_goals(
            uuid4(),
            [(None, "Climb 7a", "2027-01-01"), (None, "Ride 100 km", "2026-09-28")],
            clock=NOON_UTC,
            repository=repository,
        )

    assert raised.value.problems == {1: GoalProblem.PAST_DATE}
    assert repository.replace_calls == 0


async def test_today_anywhere_on_earth_is_not_in_the_past() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()

    await set_goals(
        user_id, [(None, "Climb 7a", "2026-09-29")], clock=NOON_UTC, repository=repository
    )

    assert repository.goals[user_id] == [Goal(text="Climb 7a", target_date=date(2026, 9, 29))]


async def test_just_after_utc_midnight_the_previous_utc_day_still_counts_as_today() -> None:
    # 00:30 UTC on 30 September is 12:30 on 29 September at UTC-12, so the 29th is someone's
    # today and must be accepted; the 28th is past everywhere.
    repository = FakeOnboardingRepository()
    clock = FakeClock(datetime(2026, 9, 30, 0, 30, tzinfo=UTC))
    user_id = uuid4()

    await set_goals(user_id, [(None, "Climb 7a", "2026-09-29")], clock=clock, repository=repository)
    with pytest.raises(InvalidGoalsError) as raised:
        await set_goals(
            user_id, [(None, "Climb 7a", "2026-09-28")], clock=clock, repository=repository
        )

    assert raised.value.problems == {0: GoalProblem.PAST_DATE}


async def test_at_11_59_utc_the_date_at_utc_minus_12_is_the_previous_day() -> None:
    clock = FakeClock(datetime(2026, 9, 30, 11, 59, tzinfo=UTC))

    assert earliest_today(clock) == date(2026, 9, 29)


async def test_at_12_00_utc_the_date_at_utc_minus_12_rolls_over() -> None:
    clock = FakeClock(datetime(2026, 9, 30, 12, 0, tzinfo=UTC))

    assert earliest_today(clock) == date(2026, 9, 30)


@pytest.mark.parametrize("bad", ["tomorrow", "2027-02-30", "30/09/2027", "2027-9-1x"])
async def test_a_date_that_is_not_an_iso_calendar_date_is_flagged(bad: str) -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(InvalidGoalsError) as raised:
        await set_goals(uuid4(), [(None, "Climb 7a", bad)], clock=NOON_UTC, repository=repository)

    assert raised.value.problems == {0: GoalProblem.BAD_DATE}
    assert repository.replace_calls == 0


async def test_every_bad_row_is_flagged_at_once() -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(InvalidGoalsError) as raised:
        await set_goals(
            uuid4(),
            [
                (None, "", "2027-01-01"),
                (None, "Fine", ""),
                (None, "x" * 201, ""),
                (None, "Late", "2020-01-01"),
            ],
            clock=NOON_UTC,
            repository=repository,
        )

    assert raised.value.problems == {
        0: GoalProblem.BLANK,
        2: GoalProblem.TOO_LONG,
        3: GoalProblem.PAST_DATE,
    }


async def test_nul_characters_are_dropped_because_postgres_text_cannot_hold_them() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()

    await set_goals(user_id, [(None, "Climb\x00 7a", "")], clock=NOON_UTC, repository=repository)

    assert repository.goals[user_id] == [Goal(text="Climb 7a", target_date=None)]


async def test_text_of_only_nul_characters_is_blank() -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(InvalidGoalsError) as raised:
        await set_goals(
            uuid4(), [(None, "\x00 \x00", "2027-01-01")], clock=NOON_UTC, repository=repository
        )

    assert raised.value.problems == {0: GoalProblem.BLANK}


async def test_saving_again_replaces_the_earlier_goals() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    await set_goals(
        user_id, [(None, "Old", ""), (None, "Removed", "")], clock=NOON_UTC, repository=repository
    )

    await set_goals(user_id, [(None, "Old", "")], clock=NOON_UTC, repository=repository)

    assert repository.goals[user_id] == [Goal(text="Old", target_date=None)]


async def test_goals_are_stored_with_their_sports_in_order() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    repository.sports[user_id] = ["climbing", "gym"]

    await set_goals(
        user_id,
        [
            ("climbing", "Send 8a+ by spring", ""),
            ("gym", "Pull-up with +10 kg", "2027-06-30"),
            (None, "Train consistently", ""),
        ],
        clock=NOON_UTC,
        repository=repository,
    )

    assert repository.goals[user_id] == [
        Goal(text="Send 8a+ by spring", sport_id="climbing"),
        Goal(text="Pull-up with +10 kg", target_date=date(2027, 6, 30), sport_id="gym"),
        Goal(text="Train consistently"),
    ]


async def test_a_goal_for_a_sport_the_user_did_not_pick_is_rejected_and_nothing_is_stored() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    repository.sports[user_id] = ["climbing"]

    with pytest.raises(UnknownGoalSportError) as raised:
        await set_goals(
            user_id,
            [("climbing", "Send 8a+", ""), ("cycling", "Ride 100 km", "")],
            clock=NOON_UTC,
            repository=repository,
        )

    assert raised.value.sport_id == "cycling"
    assert repository.replace_calls == 0


async def test_a_goal_for_a_sport_that_is_not_in_the_registry_is_rejected() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    repository.sports[user_id] = ["climbing"]

    with pytest.raises(UnknownGoalSportError):
        await set_goals(user_id, [("chess", "Win", "")], clock=NOON_UTC, repository=repository)

    assert repository.replace_calls == 0


async def test_the_sport_check_uses_the_sports_of_the_user_saving() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    other = uuid4()
    repository.sports[other] = ["climbing"]

    with pytest.raises(UnknownGoalSportError):
        await set_goals(
            user_id, [("climbing", "Send 8a+", "")], clock=NOON_UTC, repository=repository
        )

    assert repository.replace_calls == 0


async def test_rows_left_completely_empty_are_skipped() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    repository.sports[user_id] = ["climbing", "gym"]

    await set_goals(
        user_id,
        [("climbing", "", ""), ("gym", "  ", " "), (None, "Train consistently", "")],
        clock=NOON_UTC,
        repository=repository,
    )

    assert repository.goals[user_id] == [Goal(text="Train consistently")]


async def test_every_row_empty_is_no_goals() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    repository.sports[user_id] = ["climbing"]

    with pytest.raises(NoGoalsError):
        await set_goals(
            user_id, [("climbing", "", ""), (None, "", "")], clock=NOON_UTC, repository=repository
        )

    assert repository.replace_calls == 0


async def test_a_problem_is_reported_at_the_rows_posted_position_even_after_skipped_rows() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    repository.sports[user_id] = ["climbing"]

    with pytest.raises(InvalidGoalsError) as raised:
        await set_goals(
            user_id,
            [("climbing", "", ""), (None, "Late", "2020-01-01")],
            clock=NOON_UTC,
            repository=repository,
        )

    assert raised.value.problems == {1: GoalProblem.PAST_DATE}


async def test_a_forged_sport_on_an_otherwise_empty_row_is_still_rejected() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    repository.sports[user_id] = ["climbing"]

    with pytest.raises(UnknownGoalSportError):
        await set_goals(
            user_id,
            [("climbing", "Send 8a+", ""), ("cycling", "", "")],
            clock=NOON_UTC,
            repository=repository,
        )

    assert repository.replace_calls == 0


async def test_a_forged_sport_wins_over_other_row_problems() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    repository.sports[user_id] = ["climbing"]

    with pytest.raises(UnknownGoalSportError):
        await set_goals(
            user_id,
            [("cycling", "Ride", ""), ("climbing", "Late", "2020-01-01")],
            clock=NOON_UTC,
            repository=repository,
        )


@pytest.mark.parametrize("sport_id", ["Climbing", " climbing", "climbing "])
async def test_sport_ids_are_matched_exactly(sport_id: str) -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    repository.sports[user_id] = ["climbing"]

    with pytest.raises(UnknownGoalSportError):
        await set_goals(
            user_id, [(sport_id, "Send 8a+", "")], clock=NOON_UTC, repository=repository
        )

    assert repository.replace_calls == 0
