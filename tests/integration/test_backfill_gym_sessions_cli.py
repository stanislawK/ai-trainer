"""`ai-trainer backfill-gym-sessions` output and exit code (ADR-0032, ticket #377). The
command's work is covered in `test_gym_backfill.py`; this checks what it prints."""

import io
from collections.abc import Mapping, Sequence
from uuid import uuid4

from pydantic import JsonValue

from ai_trainer import cli
from ai_trainer.application.ports.gym_backfill import BackfillCandidate
from ai_trainer.domain.sports.gym.rows import GymEntryRow


class _Store:
    def __init__(self, candidates: Sequence[BackfillCandidate]) -> None:
        self._candidates = list(candidates)

    async def candidates(self) -> list[BackfillCandidate]:
        return list(self._candidates)

    async def write(self, candidate: BackfillCandidate, entries: Sequence[GymEntryRow]) -> None:
        return None


def _candidate(payload: Mapping[str, JsonValue]) -> BackfillCandidate:
    return BackfillCandidate(
        user_id=uuid4(), session_id=uuid4(), payload=payload, payload_version=1
    )


async def test_prints_the_outcome_and_exits_zero() -> None:
    output = io.StringIO()
    good = _candidate({"sport": "gym", "activities": [{"name": "Squat", "sets": 2, "reps": 5}]})

    code = await cli.backfill_gym_sessions(store=_Store([good]), output=output)

    assert code == 0
    assert output.getvalue() == (
        "backfill-gym-sessions: result=ok sessions=1 entries=1 sets=2 skipped=0\n"
    )


async def test_prints_each_skipped_session_without_its_payload_and_exits_one() -> None:
    output = io.StringIO()
    broken = _candidate({"sport": "gym", "activities": "private note"})

    code = await cli.backfill_gym_sessions(store=_Store([broken]), output=output)

    assert code == 1
    assert output.getvalue() == (
        f"backfill-gym-sessions: skipped session={broken.session_id} reason=payload_invalid\n"
        "backfill-gym-sessions: result=skipped sessions=0 entries=0 sets=0 skipped=1\n"
    )
    assert "private note" not in output.getvalue()


async def test_run_opens_its_own_engine_and_prints_the_outcome() -> None:
    output = io.StringIO()

    code = await cli.run(["backfill-gym-sessions"], output=output)

    # Tests roll back, so the database holds no committed gym session to backfill.
    assert code == 0
    assert output.getvalue() == (
        "backfill-gym-sessions: result=ok sessions=0 entries=0 sets=0 skipped=0\n"
    )


def test_the_parser_knows_backfill_gym_sessions() -> None:
    assert cli.parse_args(["backfill-gym-sessions"]).command == "backfill-gym-sessions"
