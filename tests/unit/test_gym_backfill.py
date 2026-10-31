"""The gym backfill use case (ADR-0032, ticket #377): each candidate's v1 payload is read into
typed rows through the gym's payload model; one that won't read is reported and skipped, and the
others still run."""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from pydantic import JsonValue

from ai_trainer.application.gym_backfill import backfill_gym_sessions
from ai_trainer.application.ports.gym_backfill import BackfillCandidate
from ai_trainer.domain.sports.gym.payload import GYM_PAYLOAD_VERSION
from ai_trainer.domain.sports.gym.rows import GymEntryRow


class FakeBackfillStore:
    def __init__(self, candidates: Sequence[BackfillCandidate], *, failing: UUID | None = None):
        self._candidates = list(candidates)
        self._failing = failing
        self.written: list[tuple[UUID, UUID, tuple[GymEntryRow, ...]]] = []

    async def candidates(self) -> list[BackfillCandidate]:
        return list(self._candidates)

    async def write(self, candidate: BackfillCandidate, entries: Sequence[GymEntryRow]) -> None:
        if candidate.session_id == self._failing:
            raise OSError("the database went away")
        self.written.append((candidate.user_id, candidate.session_id, tuple(entries)))


def _candidate(payload: Mapping[str, JsonValue], *, version: int = 1) -> BackfillCandidate:
    return BackfillCandidate(
        user_id=uuid4(), session_id=uuid4(), payload=payload, payload_version=version
    )


def _squat() -> dict[str, Any]:
    return {
        "sport": "gym",
        "activities": [
            {
                "name": "Squat",
                "sets": 3,
                "reps": 5,
                "load": {"original": "100kg", "value": 100, "unit": "kg", "kg": 100},
            },
            {"name": "Pull-ups", "sets": None, "reps": 8},
        ],
    }


async def test_a_v1_payload_becomes_its_entries_and_sets_for_its_own_user() -> None:
    candidate = _candidate(_squat())
    store = FakeBackfillStore([candidate])

    report = await backfill_gym_sessions(store=store)

    [(user_id, session_id, entries)] = store.written
    assert (user_id, session_id) == (candidate.user_id, candidate.session_id)
    assert [(e.name_as_typed, e.measure, len(e.sets)) for e in entries] == [
        ("Squat", "load_reps", 3),
        ("Pull-ups", "reps", 1),
    ]
    assert (report.sessions, report.entries, report.sets, report.skipped) == (1, 2, 4, ())


async def test_a_payload_that_will_not_read_is_reported_and_the_others_still_run() -> None:
    broken = _candidate({"sport": "gym", "activities": "not a list"})
    newer = _candidate(_squat(), version=GYM_PAYLOAD_VERSION + 1)
    good = _candidate(_squat())
    store = FakeBackfillStore([broken, newer, good])

    report = await backfill_gym_sessions(store=store)

    assert [session_id for _, session_id, _ in store.written] == [good.session_id]
    assert [(skip.session_id, skip.reason) for skip in report.skipped] == [
        (broken.session_id, "payload_invalid"),
        (newer.session_id, "payload_version_unsupported"),
    ]
    assert (report.sessions, report.entries) == (1, 2)


async def test_a_session_that_fails_to_write_is_reported_and_the_others_still_run() -> None:
    first, second = _candidate(_squat()), _candidate(_squat())
    store = FakeBackfillStore([first, second], failing=first.session_id)

    report = await backfill_gym_sessions(store=store)

    assert [session_id for _, session_id, _ in store.written] == [second.session_id]
    assert [(skip.session_id, skip.reason) for skip in report.skipped] == [
        (first.session_id, "write_failed")
    ]
    assert report.sessions == 1


async def test_nothing_to_backfill_reports_zeros() -> None:
    report = await backfill_gym_sessions(store=FakeBackfillStore([]))

    assert (report.sessions, report.entries, report.sets, report.skipped) == (0, 0, 0, ())
