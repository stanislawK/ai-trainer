"""The catalogue search as the shortlist's third input (PRD-0010 B37, G14, ADR-0031, ticket #429):
the trigram shortlist and the search's exercises are fused by reciprocal rank, a slow or failed
search leaves today's shortlist, and a name history knows never reaches the search."""

import asyncio
from collections.abc import Mapping, Sequence
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.exercise_candidates import SearchedExerciseNames
from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.application.ports.conversation import FixedReply
from ai_trainer.domain.exercises import ExerciseCandidate, ExerciseLabel
from ai_trainer.domain.sports.gym.payload import GymPayload
from tests.picker_support import ScriptedPicker

_USER = uuid4()


def _exercise(name: str) -> ExerciseCandidate:
    return ExerciseCandidate(id=uuid4(), name=name, equipment="dumbbell", target="delts")


_STABILITY = _exercise("Stability Ball Crunch")
_LATERAL = _exercise("Dumbbell Lateral Raise")
_FRONT = _exercise("Dumbbell Front Raise")
_REAR = _exercise("Dumbbell Rear Delt Raise")
_PRESS = _exercise("Dumbbell Shoulder Press")
_CATALOGUE = {c.id: c for c in (_STABILITY, _LATERAL, _FRONT, _REAR, _PRESS)}
_LIMITS = ResolutionLimits(near_match=0.75, candidate_floor=0.25, shortlist_size=4)
_TYPED = "rising straight arms on both sides"


class _Names:
    """Today's names port: aliases and a scripted trigram shortlist."""

    def __init__(
        self, trigram: Sequence[ExerciseCandidate] = (), aliases: Mapping[str, UUID] | None = None
    ) -> None:
        self.trigram = trigram
        self.aliases = aliases or {}
        self.shortlisted: list[int] = []

    async def match_user_aliases(
        self, user_id: UUID, *, sport_id: str, names: Sequence[str], near: float
    ) -> Mapping[str, UUID]:
        return {name: self.aliases[name] for name in names if name in self.aliases}

    async def shortlist(
        self,
        user_id: UUID,
        *,
        sport_id: str,
        name: str,
        measure: str,
        floor: float,
        limit: int,
    ) -> Sequence[ExerciseCandidate]:
        self.shortlisted.append(limit)
        return list(self.trigram)[:limit]

    async def labels(
        self, user_id: UUID, *, named: Mapping[str, UUID], near: float
    ) -> Mapping[str, ExerciseLabel]:
        return {typed: ExerciseLabel(name="Delegated") for typed in named}


class _Search:
    """The search's ranked exercise IDs. `behaviour` may sleep or raise."""

    def __init__(
        self, ranked: Sequence[ExerciseCandidate] = (), *, delay: float = 0.0, fail: bool = False
    ) -> None:
        self.ranked = ranked
        self.delay = delay
        self.fail = fail
        self.asked: list[tuple[UUID, str]] = []

    async def similar(self, user_id: UUID, name: str) -> Sequence[UUID]:
        self.asked.append((user_id, name))
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [c.id for c in self.ranked]


class _Reader:
    """The user-scoped read of the fused IDs; an unknown ID is one the athlete can't see."""

    def __init__(self, visible: Mapping[UUID, ExerciseCandidate] = _CATALOGUE) -> None:
        self.visible = visible

    async def candidates(
        self, user_id: UUID, exercise_ids: Sequence[UUID]
    ) -> Sequence[ExerciseCandidate]:
        return [self.visible[i] for i in exercise_ids if i in self.visible]


def _names(
    trigram: Sequence[ExerciseCandidate],
    search: _Search,
    *,
    inner: _Names | None = None,
    timeout: float = 1.0,
    reader: _Reader | None = None,
) -> SearchedExerciseNames:
    return SearchedExerciseNames(
        inner or _Names(trigram),
        search,
        reader or _Reader(),
        timeout_seconds=timeout,
    )


async def _shortlist(names: SearchedExerciseNames, *, limit: int = 4) -> list[ExerciseCandidate]:
    return list(
        await names.shortlist(
            _USER,
            sport_id="gym",
            name=_TYPED,
            measure="load_reps",
            floor=_LIMITS.candidate_floor,
            limit=limit,
        )
    )


async def test_rising_straight_arms_lists_dumbbell_lateral_raise_among_its_candidates() -> None:
    # Trigram finds only a stability-ball crunch; the search knows the movement.
    names = _names([_STABILITY], _Search([_LATERAL, _FRONT]))

    assert _LATERAL in await _shortlist(names)


async def test_an_exercise_in_both_lists_beats_one_in_a_single_list() -> None:
    names = _names([_STABILITY, _FRONT], _Search([_LATERAL, _FRONT]))

    shortlist = await _shortlist(names)

    assert shortlist[0] == _FRONT
    assert {c.id for c in shortlist} == {_STABILITY.id, _FRONT.id, _LATERAL.id}


async def test_the_fused_shortlist_is_cut_at_the_limit() -> None:
    names = _names([_STABILITY, _FRONT], _Search([_LATERAL, _REAR, _PRESS]))

    assert len(await _shortlist(names, limit=3)) == 3


async def test_the_search_does_not_add_an_exercise_twice_when_it_matches_two_chunks() -> None:
    names = _names([], _Search([_LATERAL, _LATERAL, _FRONT]))

    ids = [c.id for c in await _shortlist(names)]

    assert ids == [_LATERAL.id, _FRONT.id]


async def test_an_exercise_the_athlete_cannot_see_is_never_listed() -> None:
    other_athletes = _exercise("Someone Else's Custom Raise")
    visible = {c.id: c for c in (_LATERAL,)}
    names = _names([], _Search([other_athletes, _LATERAL]), reader=_Reader(visible))

    assert await _shortlist(names) == [_LATERAL]


async def test_the_search_is_asked_for_the_typed_name_and_the_acting_athlete() -> None:
    search = _Search([_LATERAL])

    await _shortlist(_names([], search))

    assert search.asked == [(_USER, _TYPED)]


async def test_a_slow_search_falls_back_to_the_trigram_shortlist() -> None:
    names = _names([_STABILITY], _Search([_LATERAL], delay=5.0), timeout=0.01)

    assert await _shortlist(names) == [_STABILITY]


async def test_a_failed_search_falls_back_to_the_trigram_shortlist() -> None:
    names = _names([_STABILITY], _Search([_LATERAL], fail=True))

    assert await _shortlist(names) == [_STABILITY]


async def test_a_search_with_no_match_leaves_the_trigram_shortlist_alone() -> None:
    names = _names([_STABILITY, _FRONT], _Search([]))

    assert await _shortlist(names) == [_STABILITY, _FRONT]


async def test_the_trigram_leg_is_read_deeper_than_the_cap() -> None:
    inner = _Names([_STABILITY])

    await _shortlist(_names([], _Search([_LATERAL]), inner=inner), limit=4)

    assert inner.shortlisted == [20]


async def test_a_failing_trigram_read_fails_the_shortlist_and_stops_the_search() -> None:
    class _Broken(_Names):
        async def shortlist(self, *args: object, **kwargs: object) -> Sequence[ExerciseCandidate]:
            await asyncio.sleep(0)  # let the search start first
            raise RuntimeError("db is down")

    search = _Search([_LATERAL], delay=5.0)

    with pytest.raises(RuntimeError, match="db is down"):
        await _shortlist(_names([], search, inner=_Broken()))

    assert search.asked  # it had started, and was cancelled rather than left running


async def test_aliases_and_labels_are_the_inner_ports() -> None:
    inner = _Names(aliases={"bench": _LATERAL.id})
    names = _names([], _Search(), inner=inner)

    assert await names.match_user_aliases(
        _USER, sport_id="gym", names=["bench", "row"], near=0.75
    ) == {"bench": _LATERAL.id}
    assert await names.labels(_USER, named={"bench": _LATERAL.id}, near=0.75) == {
        "bench": ExerciseLabel(name="Delegated")
    }


def _never_asked() -> FixedReply:
    raise AssertionError("settling reads no question")


async def test_a_name_in_the_athletes_history_makes_no_search_and_no_embedding_call() -> None:
    search = _Search([_LATERAL])
    picker = ScriptedPicker()
    names = _names([_STABILITY], search, inner=_Names(aliases={_TYPED: _LATERAL.id}))
    resolver = ResolveExercises(names, picker, question=_never_asked, limits=_LIMITS)
    payload = GymPayload.model_validate(
        {
            "activities": [
                {
                    "name": _TYPED,
                    "sets": 3,
                    "reps": 8,
                }
            ]
        }
    ).model_dump(mode="json")

    settled = GymPayload.model_validate(await resolver.settle(_USER, payload))

    assert search.asked == []
    assert picker.asked == []
    assert settled.activities[0].exercise_id == _LATERAL.id


async def test_a_log_still_drafts_when_the_search_times_out() -> None:
    # Jev is shown today's shortlist and picks from it; the slow search changes nothing.
    search = _Search([_LATERAL], delay=5.0)
    picker = ScriptedPicker({_TYPED: _STABILITY})
    names = _names([_STABILITY], search, timeout=0.01)
    resolver = ResolveExercises(names, picker, question=_never_asked, limits=_LIMITS)
    payload = GymPayload.model_validate(
        {"activities": [{"name": _TYPED, "sets": 3, "reps": 8}]}
    ).model_dump(mode="json")

    settled = GymPayload.model_validate(await resolver.settle(_USER, payload))

    assert [candidates for _, _, candidates in picker.asked] == [(_STABILITY,)]
    assert settled.activities[0].exercise_id == _STABILITY.id


async def test_jev_is_shown_the_fused_shortlist_when_the_search_answers() -> None:
    picker = ScriptedPicker()
    names = _names([_STABILITY], _Search([_LATERAL]))
    resolver = ResolveExercises(names, picker, question=_never_asked, limits=_LIMITS)
    payload = GymPayload.model_validate(
        {"activities": [{"name": _TYPED, "sets": 3, "reps": 8}]}
    ).model_dump(mode="json")

    await resolver.settle(_USER, payload)

    [(_, _, shown)] = picker.asked
    assert {c.id for c in shown} == {_STABILITY.id, _LATERAL.id}
