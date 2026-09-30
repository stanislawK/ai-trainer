"""`choose_sports` stores exactly the picked sports, validated against `SportRegistry`
(ADR-0006, ticket #74)."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.onboarding import EmptySportSelectionError, choose_sports
from ai_trainer.domain.sports.registry import UnknownSportError, default_sport_registry


class FakeOnboardingRepository:
    def __init__(self) -> None:
        self.sports: dict[UUID, list[str]] = {}
        self.replace_calls = 0

    async def replace_sports(self, user_id: UUID, sport_ids: Sequence[str]) -> None:
        self.replace_calls += 1
        self.sports[user_id] = list(sport_ids)

    async def list_sports(self, user_id: UUID) -> Sequence[str]:
        return self.sports.get(user_id, [])

    async def replace_availability(
        self, user_id: UUID, minutes_by_weekday: Mapping[int, int]
    ) -> None:
        raise NotImplementedError

    async def list_availability(self, user_id: UUID) -> Mapping[int, int]:
        raise NotImplementedError

    async def set_onboarded_at(self, user_id: UUID, when: datetime | None) -> None:
        raise NotImplementedError


async def test_choose_sports_stores_exactly_the_picked_sports() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()

    await choose_sports(
        user_id, ["climbing", "gym"], registry=default_sport_registry(), repository=repository
    )

    assert repository.sports == {user_id: ["climbing", "gym"]}


async def test_choose_sports_ignores_a_repeated_id() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()

    await choose_sports(
        user_id, ["gym", "gym"], registry=default_sport_registry(), repository=repository
    )

    assert repository.sports == {user_id: ["gym"]}


async def test_choose_sports_with_nothing_picked_stores_nothing() -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(EmptySportSelectionError):
        await choose_sports(uuid4(), [], registry=default_sport_registry(), repository=repository)

    assert repository.replace_calls == 0


async def test_choose_sports_rejects_an_unknown_id_and_stores_nothing() -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(UnknownSportError):
        await choose_sports(
            uuid4(),
            ["climbing", "chess"],
            registry=default_sport_registry(),
            repository=repository,
        )

    assert repository.replace_calls == 0
