"""`confirm_timezone` validates an IANA zone with `zoneinfo` and finishes onboarding through
the `Clock` port (PRD-0003 F6, G9, ADR-0014, ticket #77)."""

import importlib.util
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.onboarding import (
    InvalidTimezoneError,
    confirm_timezone,
    timezone_names,
)
from ai_trainer.domain.goals import Goal

FROZEN_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class FakeOnboardingRepository:
    def __init__(self) -> None:
        self.finished: dict[UUID, tuple[str, datetime]] = {}

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
        raise NotImplementedError

    async def list_availability(self, user_id: UUID) -> Mapping[int, int]:
        raise NotImplementedError

    async def replace_goals(self, user_id: UUID, goals: Sequence[Goal]) -> None:
        raise NotImplementedError

    async def list_goals(self, user_id: UUID) -> Sequence[Goal]:
        raise NotImplementedError

    async def set_onboarded_at(self, user_id: UUID, when: datetime | None) -> None:
        raise NotImplementedError

    async def finish_onboarding(self, user_id: UUID, timezone: str, when: datetime) -> None:
        self.finished[user_id] = (timezone, when)


@pytest.mark.parametrize("zone", ["Europe/Warsaw", "UTC", "America/Argentina/Buenos_Aires"])
async def test_a_valid_zone_is_stored_and_onboarded_at_comes_from_the_clock(zone: str) -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()

    await confirm_timezone(user_id, zone, clock=FakeClock(), repository=repository)

    assert repository.finished == {user_id: (zone, FROZEN_NOW)}


async def test_surrounding_whitespace_is_trimmed() -> None:
    repository = FakeOnboardingRepository()
    user_id = uuid4()

    await confirm_timezone(user_id, "  Europe/Warsaw ", clock=FakeClock(), repository=repository)

    assert repository.finished[user_id][0] == "Europe/Warsaw"


@pytest.mark.parametrize(
    "zone",
    [
        "Mars/Olympus",
        "",
        "   ",
        "europe/warsaw",
        "../etc/passwd",
        "/etc/localtime",
        "Europe\x00",
        "Poland",
        "Factory",
    ],
)
async def test_an_invalid_zone_raises_and_writes_nothing(zone: str) -> None:
    repository = FakeOnboardingRepository()

    with pytest.raises(InvalidTimezoneError):
        await confirm_timezone(uuid4(), zone, clock=FakeClock(), repository=repository)

    assert repository.finished == {}


def test_the_zone_list_does_not_depend_on_the_host_having_a_zone_database() -> None:
    # The slim runtime image ships no /usr/share/zoneinfo, so the `tzdata` package must be a
    # dependency on every platform, or the list would be empty and every confirm would fail.
    assert importlib.util.find_spec("tzdata") is not None
    assert {"UTC", "Europe/Warsaw"} <= set(timezone_names())
