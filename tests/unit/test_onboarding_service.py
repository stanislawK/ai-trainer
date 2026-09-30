"""`choose_sports` stores exactly the picked sports, validated against `SportRegistry`
(ADR-0006, ticket #74)."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.onboarding import (
    DroppedSportsHaveGoalsError,
    EmptySportSelectionError,
    InvalidGoalChoiceError,
    choose_sports,
)
from ai_trainer.domain.goals import Goal
from ai_trainer.domain.sports.registry import UnknownSportError, default_sport_registry


class FakeOnboardingRepository:
    def __init__(self) -> None:
        self.sports: dict[UUID, list[str]] = {}
        self.goals: dict[UUID, list[Goal]] = {}
        self.replace_calls = 0

    async def replace_sports(
        self,
        user_id: UUID,
        sport_ids: Sequence[str],
        *,
        general_goals_of: Sequence[str] = (),
        delete_goals_of: Sequence[str] = (),
    ) -> None:
        self.replace_calls += 1
        self.sports[user_id] = list(sport_ids)
        kept = [
            goal.model_copy(update={"sport_id": None})
            if goal.sport_id in general_goals_of
            else goal
            for goal in self.goals.get(user_id, [])
            if goal.sport_id not in delete_goals_of
        ]
        self.goals[user_id] = kept

    async def list_sports(self, user_id: UUID) -> Sequence[str]:
        return self.sports.get(user_id, [])

    async def replace_availability(
        self, user_id: UUID, minutes_by_weekday: Mapping[int, int]
    ) -> None:
        raise NotImplementedError

    async def list_availability(self, user_id: UUID) -> Mapping[int, int]:
        raise NotImplementedError

    async def replace_goals(self, user_id: UUID, goals: Sequence[Goal]) -> None:
        raise NotImplementedError

    async def list_goals(self, user_id: UUID) -> Sequence[Goal]:
        return self.goals.get(user_id, [])

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


def _seeded() -> tuple[FakeOnboardingRepository, UUID]:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    repository.sports[user_id] = ["climbing", "gym"]
    repository.goals[user_id] = [
        Goal(text="Send 8a+", sport_id="climbing"),
        Goal(text="Pull-up +10 kg", sport_id="gym"),
        Goal(text="Deadlift 150 kg", sport_id="gym"),
        Goal(text="Stay consistent"),
    ]
    return repository, user_id


async def test_dropping_a_sport_with_goals_asks_first_and_stores_nothing() -> None:
    repository, user_id = _seeded()

    with pytest.raises(DroppedSportsHaveGoalsError) as raised:
        await choose_sports(
            user_id, ["climbing"], registry=default_sport_registry(), repository=repository
        )

    assert raised.value.dropped == {
        "gym": [
            Goal(text="Pull-up +10 kg", sport_id="gym"),
            Goal(text="Deadlift 150 kg", sport_id="gym"),
        ]
    }
    assert repository.replace_calls == 0
    assert repository.sports[user_id] == ["climbing", "gym"]


async def test_keep_turns_the_dropped_sports_goals_into_general_goals() -> None:
    repository, user_id = _seeded()

    await choose_sports(
        user_id,
        ["climbing"],
        registry=default_sport_registry(),
        repository=repository,
        goal_choices={"gym": "keep"},
    )

    assert repository.sports[user_id] == ["climbing"]
    assert repository.goals[user_id] == [
        Goal(text="Send 8a+", sport_id="climbing"),
        Goal(text="Pull-up +10 kg"),
        Goal(text="Deadlift 150 kg"),
        Goal(text="Stay consistent"),
    ]


async def test_delete_removes_the_dropped_sports_goals_and_leaves_the_rest() -> None:
    repository, user_id = _seeded()

    await choose_sports(
        user_id,
        ["climbing"],
        registry=default_sport_registry(),
        repository=repository,
        goal_choices={"gym": "delete"},
    )

    assert repository.sports[user_id] == ["climbing"]
    assert repository.goals[user_id] == [
        Goal(text="Send 8a+", sport_id="climbing"),
        Goal(text="Stay consistent"),
    ]


async def test_each_dropped_sport_gets_its_own_choice() -> None:
    repository, user_id = _seeded()

    await choose_sports(
        user_id,
        ["cycling"],
        registry=default_sport_registry(),
        repository=repository,
        goal_choices={"climbing": "keep", "gym": "delete"},
    )

    assert repository.goals[user_id] == [Goal(text="Send 8a+"), Goal(text="Stay consistent")]


@pytest.mark.parametrize("choices", [{}, {"gym": "archive"}, {"gym": ""}, {"climbing": "keep"}])
async def test_a_missing_or_unknown_choice_stores_nothing(choices: dict[str, str]) -> None:
    repository, user_id = _seeded()

    with pytest.raises(InvalidGoalChoiceError) as raised:
        await choose_sports(
            user_id,
            ["cycling"] if "climbing" in choices else ["climbing"],
            registry=default_sport_registry(),
            repository=repository,
            goal_choices=choices,
        )

    assert raised.value.sport_ids
    assert repository.replace_calls == 0
    assert len(repository.goals[user_id]) == 4


async def test_dropping_a_sport_without_goals_needs_no_choice() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()
    repository.sports[user_id] = ["climbing", "gym"]
    repository.goals[user_id] = [Goal(text="Send 8a+", sport_id="climbing")]

    await choose_sports(
        user_id, ["climbing"], registry=default_sport_registry(), repository=repository
    )

    assert repository.sports[user_id] == ["climbing"]
    assert repository.goals[user_id] == [Goal(text="Send 8a+", sport_id="climbing")]


async def test_a_choice_for_a_sport_that_is_not_dropped_is_ignored() -> None:
    repository, user_id = _seeded()

    await choose_sports(
        user_id,
        ["climbing", "gym"],
        registry=default_sport_registry(),
        repository=repository,
        goal_choices={"gym": "delete"},
    )

    assert len(repository.goals[user_id]) == 4


@pytest.mark.parametrize(
    ("picked", "choices"),
    [
        (["climbing", "gym"], {"gym": "archive"}),
        (["climbing"], {"gym": "keep", "cycling": "KEEP"}),
    ],
)
async def test_an_unknown_choice_is_rejected_even_when_its_sport_needs_none(
    picked: list[str], choices: dict[str, str]
) -> None:
    repository, user_id = _seeded()

    with pytest.raises(InvalidGoalChoiceError):
        await choose_sports(
            user_id,
            picked,
            registry=default_sport_registry(),
            repository=repository,
            goal_choices=choices,
        )

    assert repository.replace_calls == 0
