"""The catalogue search as the shortlist's third input (PRD-0010 B37, G14, ADR-0031, ticket #429):
the trigram shortlist and the search's exercises are fused by reciprocal rank, a slow or failed
search leaves today's shortlist, and a name history knows never reaches the search."""

import asyncio
from collections.abc import Mapping, Sequence
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from ai_trainer.application.exercise_candidates import (
    ANSWER_MEMORY,
    FAILURE_MEMORY,
    ExerciseSearchError,
    SearchedExerciseNames,
)
from ai_trainer.application.exercise_resolution import ResolutionLimits, ResolveExercises
from ai_trainer.application.ports.conversation import FixedReply
from ai_trainer.domain.exercises import ExerciseCandidate, ExerciseLabel
from ai_trainer.domain.sports.gym.payload import GymPayload
from tests.ingestion_support import FrozenClock
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
    """The user-scoped read of the fused IDs; an unknown ID is one the athlete can't see. As the
    port promises, the entry's own measure comes first and the given order otherwise."""

    def __init__(
        self,
        visible: Mapping[UUID, ExerciseCandidate] = _CATALOGUE,
        measures: Mapping[UUID, str] | None = None,
    ) -> None:
        self.visible = visible
        self.measures = measures or {}
        self.asked: list[tuple[list[UUID], str]] = []

    async def candidates(
        self, user_id: UUID, exercise_ids: Sequence[UUID], *, measure: str
    ) -> Sequence[ExerciseCandidate]:
        self.asked.append((list(exercise_ids), measure))
        found = [self.visible[i] for i in exercise_ids if i in self.visible]
        return sorted(found, key=lambda c: self.measures.get(c.id, "load_reps") != measure)


_NAME_MATCH = 0.5


def _names(
    trigram: Sequence[ExerciseCandidate],
    search: _Search,
    *,
    inner: _Names | None = None,
    timeout: float = 1.0,
    reader: _Reader | None = None,
    clock: FrozenClock | None = None,
    remembered: int = 256,
) -> SearchedExerciseNames:
    return SearchedExerciseNames(
        inner or _Names(trigram),
        search,
        reader or _Reader(),
        timeout_seconds=timeout,
        name_match=_NAME_MATCH,
        clock=clock or FrozenClock(),
        remembered=remembered,
    )


def _scored(candidate: ExerciseCandidate, score: float) -> ExerciseCandidate:
    return candidate.model_copy(update={"name_score": score})


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


async def test_an_exercise_only_the_search_finds_joins_the_shortlist() -> None:
    # Fusion arithmetic only, on scripted rankings. Whether the real search ranks the lateral raise
    # high enough for "rising straight arms on both sides" is measured, not tested (ADR-0031, #429).
    names = _names([_STABILITY], _Search([_LATERAL, _FRONT]))

    assert _LATERAL in await _shortlist(names)


async def test_search_leads_when_no_name_matches() -> None:
    # #508: a paraphrase's best trigram match is a loose one (0.34 for the crunch), so the
    # search's order leads and the trigram list follows. Fused evenly, the lateral raise would be
    # fifth, behind the crunch, and cut.
    crunch = _scored(_STABILITY, 0.34)
    names = _names([crunch], _Search([_FRONT, _REAR, _PRESS, _LATERAL]))

    shortlist = await _shortlist(names)

    assert [c.id for c in shortlist] == [_FRONT.id, _REAR.id, _PRESS.id, _LATERAL.id]


async def test_rrf_kept_when_a_name_matches() -> None:
    # A typed name that matches a catalogue name (Pull-Up at 0.70) keeps the even fusion, so the
    # match stays near the top.
    matched = _scored(_STABILITY, 0.7)
    names = _names([matched], _Search([_FRONT, _REAR, _PRESS, _LATERAL]))

    shortlist = await _shortlist(names)

    assert shortlist[0].id == _STABILITY.id
    assert _LATERAL.id not in {c.id for c in shortlist}


async def test_one_matching_name_among_loose_ones_keeps_the_fusion() -> None:
    names = _names(
        [_scored(_STABILITY, 0.3), _scored(_FRONT, 0.5)], _Search([_REAR, _PRESS, _LATERAL])
    )

    shortlist = await _shortlist(names)

    assert [c.id for c in shortlist][:2] == [_STABILITY.id, _REAR.id]


async def test_when_the_search_leads_the_entrys_own_measure_still_comes_first() -> None:
    plank = _exercise("Plank")
    reader = _Reader({**_CATALOGUE, plank.id: plank}, measures={plank.id: "time"})
    names = _names(
        [_scored(_STABILITY, 0.34)],
        _Search([plank, _FRONT, _REAR, _LATERAL]),
        reader=reader,
    )

    shortlist = await _shortlist(names)

    assert [c.id for c in shortlist] == [_FRONT.id, _REAR.id, _LATERAL.id, _STABILITY.id]


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


async def _ask(names: SearchedExerciseNames, user_id: UUID, name: str) -> None:
    await names.shortlist(
        user_id, sport_id="gym", name=name, measure="load_reps", floor=0.25, limit=4
    )


async def test_the_card_reuses_the_search_jev_was_just_shown() -> None:
    # Settling asks for the shortlist, then the card asks again for the same name: one embedding.
    search = _Search([_LATERAL])
    names = _names([_STABILITY], search)

    await _ask(names, _USER, _TYPED)
    await _ask(names, _USER, _TYPED)

    assert search.asked == [(_USER, _TYPED)]


async def test_another_athlete_or_another_name_searches_again() -> None:
    search = _Search([_LATERAL])
    names = _names([], search)
    other = uuid4()

    await _ask(names, _USER, _TYPED)
    await _ask(names, other, _TYPED)
    await _ask(names, _USER, "face pull")

    assert search.asked == [(_USER, _TYPED), (other, _TYPED), (_USER, "face pull")]


async def test_a_failed_search_is_remembered_so_the_card_does_not_wait_it_out_again() -> None:
    # Jev was shown the trigram list, so the card shows the same one, with no second timeout.
    search = _Search([_LATERAL], fail=True)
    names = _names([_STABILITY], search)

    await _ask(names, _USER, _TYPED)
    search.fail = False
    shown_on_the_card = await _shortlist(names)

    assert len(search.asked) == 1
    assert shown_on_the_card == [_STABILITY]


async def test_a_failure_is_forgotten_after_the_turn_so_it_cannot_stick_to_a_name() -> None:
    clock = FrozenClock()
    search = _Search([_LATERAL], fail=True)
    names = _names([_STABILITY], search, clock=clock)

    await _ask(names, _USER, _TYPED)  # Jev's shortlist: fails
    clock.advance(FAILURE_MEMORY - timedelta(seconds=1))
    await _ask(names, _USER, _TYPED)  # the card's, in the same turn: served from memory
    clock.advance(timedelta(seconds=2))
    search.fail = False
    later = await _shortlist(names)  # a later log of the name, even with no card: searched again

    assert len(search.asked) == 2
    assert _LATERAL in later


async def test_an_answer_is_remembered_for_a_while_and_then_searched_afresh() -> None:
    # After a catalogue re-ingest an old answer must not be served for days.
    clock = FrozenClock()
    search = _Search([_LATERAL])
    names = _names([], search, clock=clock)

    await _ask(names, _USER, _TYPED)
    clock.advance(ANSWER_MEMORY - timedelta(seconds=1))
    await _ask(names, _USER, _TYPED)
    assert len(search.asked) == 1
    clock.advance(timedelta(seconds=2))
    await _ask(names, _USER, _TYPED)

    assert len(search.asked) == 2


async def test_an_exercise_of_another_measure_does_not_take_a_slot_from_the_entrys_own() -> None:
    plank = _exercise("Plank")
    catalogue = {**_CATALOGUE, plank.id: plank}
    reader = _Reader(catalogue, measures={plank.id: "time"})
    # The search ranks the timed plank first; the entry is sets and reps (load_reps).
    names = _names(
        [_STABILITY, _FRONT, _REAR],
        _Search([plank, _LATERAL]),
        reader=reader,
    )

    shortlist = await _shortlist(names, limit=4)

    # Fused: crunch, plank (tied with it), front raise, lateral raise (tied), rear raise. The
    # plank would have been second; behind the entry's own measure it falls out of the four.
    assert [c.name for c in shortlist] == [
        "Stability Ball Crunch",
        "Dumbbell Front Raise",
        "Dumbbell Lateral Raise",
        "Dumbbell Rear Delt Raise",
    ]
    assert plank not in shortlist


async def test_every_fused_exercise_is_read_with_the_entrys_measure_before_the_cut() -> None:
    reader = _Reader()
    names = _names([_STABILITY, _FRONT], _Search([_LATERAL, _REAR, _PRESS]), reader=reader)

    await _shortlist(names, limit=2)

    [(asked, measure)] = reader.asked
    assert measure == "load_reps"
    assert set(asked) == {_STABILITY.id, _FRONT.id, _LATERAL.id, _REAR.id, _PRESS.id}


async def test_the_reason_a_search_was_unavailable_is_logged_without_the_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Unusable(_Search):
        async def similar(self, user_id: UUID, name: str) -> Sequence[UUID]:
            raise ExerciseSearchError("embedding_unavailable")

    with caplog.at_level("WARNING"):
        await _shortlist(_names([_STABILITY], _Unusable()))

    [record] = caplog.records
    assert "embedding_unavailable" in record.getMessage()
    assert _TYPED not in record.getMessage()


async def test_a_timed_out_search_is_not_waited_for_twice() -> None:
    search = _Search([_LATERAL], delay=5.0)
    names = _names([_STABILITY], search, timeout=0.01)

    await _ask(names, _USER, _TYPED)
    await _ask(names, _USER, _TYPED)

    assert len(search.asked) == 1


async def test_a_failing_read_of_the_fused_exercises_falls_back_to_the_trigram_shortlist() -> None:
    class _Broken(_Reader):
        async def candidates(
            self, user_id: UUID, exercise_ids: Sequence[UUID], *, measure: str
        ) -> Sequence[ExerciseCandidate]:
            raise RuntimeError("db is down")

    names = _names([_STABILITY], _Search([_LATERAL]), reader=_Broken())

    assert await _shortlist(names) == [_STABILITY]


async def test_only_the_most_recent_searches_are_remembered() -> None:
    search = _Search([_LATERAL])
    names = _names([], search, remembered=2)

    for typed in ("one", "two", "three", "one"):
        await _ask(names, _USER, typed)

    assert [name for _, name in search.asked] == ["one", "two", "three", "one"]


async def test_a_recently_used_search_stays_remembered() -> None:
    search = _Search([_LATERAL])
    names = _names([], search, remembered=2)

    for typed in ("one", "two", "one", "three", "one"):
        await _ask(names, _USER, typed)

    assert [name for _, name in search.asked] == ["one", "two", "three"]


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


async def test_only_the_name_history_does_not_know_reaches_the_search() -> None:
    search = _Search([_FRONT])
    picker = ScriptedPicker()
    names = _names([_STABILITY], search, inner=_Names(aliases={_TYPED: _LATERAL.id}))
    resolver = ResolveExercises(names, picker, question=_never_asked, limits=_LIMITS)
    payload = GymPayload.model_validate(
        {
            "activities": [
                {"name": _TYPED, "sets": 3, "reps": 8},
                {"name": "front raises", "sets": 3, "reps": 10},
            ]
        }
    ).model_dump(mode="json")

    settled = GymPayload.model_validate(await resolver.settle(_USER, payload))

    assert search.asked == [(_USER, "front raises")]
    assert [name for _, name, _ in picker.asked] == ["front raises"]
    assert settled.activities[0].exercise_id == _LATERAL.id
    assert settled.activities[1].exercise_id is None  # Jev wasn't sure, so the card asks


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
