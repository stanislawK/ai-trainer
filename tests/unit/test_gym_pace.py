"""A timed distance's pace, worked out in code and never stored (PRD 0013 B42, ADR-0032
invariant 6, ticket #626): per 500 m on a rowing machine or ski erg, per km otherwise."""

import pytest

from ai_trainer.domain.sports.gym.pace import (
    clock_text,
    pace_per_split,
    pace_s_per_km,
    speed_kmh,
    split_of,
)
from ai_trainer.domain.sports.gym.payload import GymSet


def test_rower_2000m_in_7_50_reads_1_57_5_per_500m() -> None:
    rowed = GymSet(distance_m=2000, duration_s=470)

    split = split_of("rower")

    assert split == "500m"
    assert pace_s_per_km(rowed) == 235
    assert pace_per_split(rowed, split) == 117.5
    assert clock_text(117.5) == "1:57.5"


def test_run_5km_in_25_00_reads_5_00_per_km() -> None:
    ran = GymSet(distance_m=5000, duration_s=1500)

    split = split_of("run")

    assert split == "km"
    assert clock_text(pace_per_split(ran, split) or 0) == "5:00"


@pytest.mark.parametrize(
    "gym_set",
    [
        GymSet(duration_s=470),
        GymSet(distance_m=2000),
        GymSet(distance_m=0, duration_s=470),
        GymSet(distance_m=2000, duration_s=0),
        GymSet(),
    ],
    ids=["time only", "distance only", "zero distance", "zero time", "blank"],
)
def test_pace_none_when_distance_or_time_is_zero_or_missing(gym_set: GymSet) -> None:
    assert pace_s_per_km(gym_set) is None
    assert pace_per_split(gym_set, "km") is None
    assert speed_kmh(gym_set) is None


def test_speed_is_km_per_hour_so_faster_is_higher() -> None:
    assert speed_kmh(GymSet(distance_m=2000, duration_s=480)) == 15.0
    assert speed_kmh(GymSet(distance_m=2000, duration_s=470)) == pytest.approx(15.319, abs=1e-3)


@pytest.mark.parametrize(
    ("names", "split"),
    [
        (("Rowing Machine",), "500m"),
        (("rower",), "500m"),
        (("SkiErg",), "500m"),
        (("ergometer",), "500m"),
        (("wiosłowanie",), "500m"),
        (("wioślarz",), "500m"),
        (("WIOSŁOWANIE",), "500m"),
        (("ergometr wioślarski",), "500m"),
        (("Run", "rowing"), "500m"),
        (("treadmill",), "km"),
        (("bike", None), "km"),
        ((), "km"),
    ],
)
def test_split_is_500m_for_rowing_names_else_km(names: tuple[str | None, ...], split: str) -> None:
    assert split_of(*names) == split


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (470, "7:50"),
        (117.5, "1:57.5"),
        (120, "2:00"),
        (105, "1:45"),
        (59.96, "1:00"),
        (5, "0:05"),
        (3750, "1:02:30"),
        (3600.4, "1:00:00.4"),
    ],
)
def test_clock_text_shows_tenths_only_when_they_are_not_zero(seconds: float, text: str) -> None:
    assert clock_text(seconds) == text
