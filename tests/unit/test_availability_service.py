"""`set_availability` stores the days the athlete can train, validated 0-600 minutes with at
least one day above zero (PRD-0003 B10, ADR-0006, ticket #75)."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.onboarding import (
    InvalidAvailabilityError,
    NoAvailabilityError,
    set_availability,
)
from ai_trainer.domain.goals import Goal


class FakeOnboardingRepository:
    def __init__(self) -> None:
        self.availability: dict[UUID, dict[int, int]] = {}
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
        raise NotImplementedError

    async def replace_availability(
        self, user_id: UUID, minutes_by_weekday: Mapping[int, int]
    ) -> None:
        self.replace_calls += 1
        self.availability[user_id] = dict(minutes_by_weekday)

    async def list_availability(self, user_id: UUID) -> Mapping[int, int]:
        return self.availability.get(user_id, {})

    async def replace_goals(self, user_id: UUID, goals: Sequence[Goal]) -> None:
        raise NotImplementedError

    async def list_goals(self, user_id: UUID) -> Sequence[Goal]:
        raise NotImplementedError

    async def set_onboarded_at(self, user_id: UUID, when: datetime | None) -> None:
        raise NotImplementedError

    async def finish_onboarding(self, user_id: UUID, timezone: str, when: datetime) -> None:
        raise NotImplementedError


async def test_set_availability_stores_only_days_above_zero() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()

    await set_availability(
        user_id,
        {0: "60", 1: "0", 2: "90", 3: "", 4: "0", 5: "180", 6: "0"},
        repository=repository,
    )

    assert repository.availability == {user_id: {0: 60, 2: 90, 5: 180}}


async def test_set_availability_accepts_the_600_minute_ceiling() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()

    await set_availability(user_id, {0: "600"}, repository=repository)

    assert repository.availability == {user_id: {0: 600}}


async def test_zero_on_every_day_stores_nothing() -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(NoAvailabilityError):
        await set_availability(uuid4(), {d: "0" for d in range(7)}, repository=repository)

    assert repository.replace_calls == 0


async def test_blank_on_every_day_counts_as_zero() -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(NoAvailabilityError):
        await set_availability(uuid4(), {d: "" for d in range(7)}, repository=repository)

    assert repository.replace_calls == 0


@pytest.mark.parametrize(
    "bad",
    [
        "601",
        "-1",
        "-30",
        "abc",
        "1.5",
        "9999999999999999999",
        "+5",
        "1_0",
        "٦٠",
        "\uff10\uff15",
        "-0",
    ],
)
async def test_an_out_of_range_or_non_integer_value_names_its_day_and_stores_nothing(
    bad: str,
) -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(InvalidAvailabilityError) as raised:
        await set_availability(uuid4(), {0: "60", 3: bad}, repository=repository)

    assert raised.value.weekdays == frozenset({3})
    assert repository.replace_calls == 0


async def test_every_bad_day_is_reported_together() -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(InvalidAvailabilityError) as raised:
        await set_availability(uuid4(), {0: "601", 1: "-5", 2: "30"}, repository=repository)

    assert raised.value.weekdays == frozenset({0, 1})


async def test_an_unknown_weekday_is_rejected() -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(InvalidAvailabilityError):
        await set_availability(uuid4(), {7: "30"}, repository=repository)

    assert repository.replace_calls == 0


async def test_saving_again_replaces_the_earlier_days() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    await set_availability(user_id, {0: "60", 2: "90"}, repository=repository)

    await set_availability(user_id, {4: "45"}, repository=repository)

    assert repository.availability == {user_id: {4: 45}}
