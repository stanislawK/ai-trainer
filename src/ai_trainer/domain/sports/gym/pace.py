"""A timed distance's pace and speed (PRD 0013 B42, ADR-0032 invariant 6, ticket #626), worked
out in code from a set holding both a distance and a time, and never stored. A set with only one
of them, or with either at 0, has none: a pace is never estimated.

A rowing machine or ski erg reads per 500 m, anything else per km: the split comes from the
exercise's canonical or typed name, owner-chosen at #626's plan gate."""

import re
from collections.abc import Mapping
from typing import Literal, Protocol

# Whole words of a case-folded name that make its pace read per 500 m: rowing ("row",
# "rower", "rowing"), an erg ("ski erg", "skierg", "ergometer", "ergometr") or Polish rowing
# ("wiosłowanie", "wioślarz"). Whole words, so "throw", "arrow" or "narrow" never count
# (#626 code review). A lift such as "barbell row" may match, but it holds no distance with a
# time, so it never has a pace to show.
_PER_500M = re.compile(r"\b(?:row|rower|rowing|(?:ski[ -]?)?erg|ergomet\w*|wios[łl]\w*|wiośl\w*)\b")

type Split = Literal["500m", "km"]
_SPLIT_M: Mapping[Split, int] = {"500m": 500, "km": 1000}


class TimedDistance(Protocol):
    """A set as both the payload and the logged rows hold it."""

    @property
    def duration_s(self) -> int | None: ...

    @property
    def distance_m(self) -> float | None: ...


def split_of(*names: str | None) -> Split:
    """`500m` when any name reads as a rowing machine or ski erg, else `km`."""
    folded = [name.casefold() for name in names if name]
    return "500m" if any(_PER_500M.search(name) for name in folded) else "km"


def pace_s_per_km(gym_set: TimedDistance) -> float | None:
    """`duration_s / (distance_m / 1000)`, lower is faster; `None` without both above 0."""
    if not gym_set.duration_s or not gym_set.distance_m:
        return None
    return gym_set.duration_s / (gym_set.distance_m / 1000)


def pace_per_split(gym_set: TimedDistance, split: Split) -> float | None:
    """Seconds per 500 m or per km."""
    per_km = pace_s_per_km(gym_set)
    return None if per_km is None else per_km * _SPLIT_M[split] / 1000


def speed_kmh(gym_set: TimedDistance) -> float | None:
    """The same set as km/h, so faster plots higher."""
    per_km = pace_s_per_km(gym_set)
    return None if per_km is None else 3600 / per_km


def clock_text(seconds: float) -> str:
    """A clock reading: "7:50", "1:02:30", and tenths only when they aren't zero: "1:57.5"."""
    whole, tenths = divmod(round(seconds * 10), 10)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    text = f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"
    return f"{text}.{tenths}" if tenths else text
