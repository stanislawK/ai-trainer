"""A fake `ExercisePickerPort` for unit and integration tests (ADR-0031, ticket #399)."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from ai_trainer.domain.exercises import ExerciseCandidate


class ScriptedPicker:
    """Picks the candidate scripted for a typed name, or none. Records every call."""

    decision_id = "exercise_shortlist"
    decision_version = 1

    def __init__(self, picks: Mapping[str, ExerciseCandidate] | None = None) -> None:
        self.picks = picks or {}
        self.asked: list[tuple[UUID, str, tuple[ExerciseCandidate, ...]]] = []

    async def pick(
        self, user_id: UUID, name: str, candidates: Sequence[ExerciseCandidate]
    ) -> ExerciseCandidate | None:
        self.asked.append((user_id, name, tuple(candidates)))
        return self.picks.get(name)
