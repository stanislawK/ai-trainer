"""`ExercisePickerPort` on Jev (ADR-0031, ADR-0020, ticket #399): the `exercise_shortlist` set
asks which catalogue candidate a typed gym name is, or "none of these". The candidates are the
options, named by their catalogue ID and described by their name, equipment and target, so Jev
reads words and code reads IDs. The athlete still confirms the draft, and only then is an alias
stored (invariant 1)."""

from collections.abc import Mapping, Sequence
from uuid import UUID

from pydantic import BaseModel

from ai_trainer.application.ports.decision_gateway import DecisionGatewayPort
from ai_trainer.domain.exercises import ExerciseCandidate
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.llm.decisions.loader import DecisionSet, DecisionSetLoader

EXERCISE_SHORTLIST_ID = "exercise_shortlist"
EXERCISE_SHORTLIST_VERSION = 1
_QUESTION = "exercise"
NONE_OF_THESE = "none"


def _description(candidate: ExerciseCandidate) -> str:
    detail = ", ".join(part for part in (candidate.equipment, candidate.target) if part)
    return f"{candidate.name} ({detail})" if detail else candidate.name


def exercise_shortlist_set(
    candidates: Sequence[ExerciseCandidate],
    *,
    loader: DecisionSetLoader | None = None,
    version: int = EXERCISE_SHORTLIST_VERSION,
) -> DecisionSet:
    """The question over these candidates, each an option named by its ID."""
    return (loader or DecisionSetLoader()).load(
        EXERCISE_SHORTLIST_ID,
        version,
        added_options={
            _QUESTION: {str(candidate.id): _description(candidate) for candidate in candidates}
        },
    )


def confident_pick(
    output: BaseModel, probabilities: Mapping[str, Mapping[str, float]], *, threshold: float
) -> str | None:
    """The option Jev picked when it is not "none" and its own probability, read from the
    distribution by name and never from the library's pick alone, is at or above `threshold`."""
    option: str = getattr(output, _QUESTION)
    if option == NONE_OF_THESE:
        return None
    if probabilities.get(_QUESTION, {}).get(option, 0.0) < threshold:
        return None
    return option


class JevExercisePicker:
    """Implements `ExercisePickerPort`. The state is the typed name alone: no identity, no
    history and no profile reach Jev (ADR-0020 data retention)."""

    decision_id = EXERCISE_SHORTLIST_ID
    decision_version = EXERCISE_SHORTLIST_VERSION

    def __init__(
        self,
        decisions: DecisionGatewayPort,
        *,
        threshold: float,
        loader: DecisionSetLoader | None = None,
    ) -> None:
        self._decisions = decisions
        self._threshold = threshold
        self._loader = loader or DecisionSetLoader()

    async def pick(
        self, user_id: UUID, name: str, candidates: Sequence[ExerciseCandidate]
    ) -> ExerciseCandidate | None:
        if not candidates:
            return None
        try:
            decision_set = exercise_shortlist_set(candidates, loader=self._loader)
            result = await self._decisions.decide(
                user_id=user_id,
                decision_id=decision_set.id,
                decision_version=decision_set.version,
                output_type=decision_set.output_type,
                state=name.strip(),
            )
        except Exception:
            # A failure the gateway did not map to an outcome (its row write, say): the card asks.
            return None
        if result.outcome is not LlmCallOutcome.SUCCESS or result.output is None:
            return None
        option = confident_pick(result.output, result.probabilities, threshold=self._threshold)
        return next((c for c in candidates if str(c.id) == option), None)
