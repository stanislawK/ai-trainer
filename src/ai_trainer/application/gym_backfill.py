import logging
from typing import Literal, NamedTuple
from uuid import UUID

from pydantic import ValidationError

from ai_trainer.application.ports.gym_backfill import BackfillCandidate, GymBackfillPort
from ai_trainer.domain.sports.gym.payload import GYM_PAYLOAD_VERSION, GymPayload
from ai_trainer.domain.sports.gym.rows import GymEntryRow, entries_of

logger = logging.getLogger(__name__)

type SkipReason = Literal["payload_invalid", "payload_version_unsupported", "write_failed"]


class SkippedSession(NamedTuple):
    session_id: UUID
    reason: SkipReason


class BackfillReport(NamedTuple):
    sessions: int
    entries: int
    sets: int
    skipped: tuple[SkippedSession, ...]


async def backfill_gym_sessions(*, store: GymBackfillPort) -> BackfillReport:
    """Writes each gym session's v1 payload as typed rows (ADR-0032). A session that won't read
    or write is reported and skipped; the others still run. Only ids and a reason are kept:
    nothing of the payload reaches the report or the log (ADR-0030)."""
    sessions = entries = sets = 0
    skipped: list[SkippedSession] = []
    for candidate in await store.candidates():
        rows = _rows_of(candidate)
        if isinstance(rows, str):
            skipped.append(SkippedSession(candidate.session_id, rows))
            continue
        try:
            await store.write(candidate, rows)
        except Exception as exc:  # one session's failure must not stop the others
            # The type names the cause without a payload or a message, which may carry content.
            logger.warning(
                "gym backfill: write failed for session %s: %s",
                candidate.session_id,
                type(exc).__name__,
            )
            skipped.append(SkippedSession(candidate.session_id, "write_failed"))
            continue
        sessions += 1
        entries += len(rows)
        sets += sum(len(entry.sets) for entry in rows)
    return BackfillReport(sessions, entries, sets, tuple(skipped))


def _rows_of(candidate: BackfillCandidate) -> tuple[GymEntryRow, ...] | SkipReason:
    """The payload read through the gym's payload model, the one place that reads it (the
    payload is at v1, so there is no older shape to upcast); a newer version isn't guessed."""
    if candidate.payload_version > GYM_PAYLOAD_VERSION:
        return "payload_version_unsupported"
    try:
        return entries_of(GymPayload.model_validate(candidate.payload))
    except ValidationError:
        return "payload_invalid"
