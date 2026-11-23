"""`JevGoalKind` (PRD 0013 B45, ADR-0028 #618 amendment, ticket #625): Jev picks one of six kinds
for a goal's text; a pick below `goal_kind_threshold`, `none`, or any failure is no kind, and
onboarding goes on."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from ai_trainer.application.ports.decision_gateway import DecisionResult
from ai_trainer.domain.goals import GoalKind
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.llm.goal_kind import (
    GOAL_KIND_ID,
    GOAL_KIND_VERSION,
    NO_KIND,
    GoalKindState,
    JevGoalKind,
    goal_kind_set,
)
from ai_trainer.settings import Settings

_USER = uuid4()
_THRESHOLD = 0.7
_DECISIONS = Path(__file__).parents[2] / "src" / "ai_trainer" / "llm" / "decisions"


class _Decisions:
    """A fake `DecisionGatewayPort` that picks `picked` at `probability`, or fails."""

    def __init__(
        self,
        picked: str | None = "mobility",
        probability: float = 0.9,
        outcome: LlmCallOutcome = LlmCallOutcome.SUCCESS,
        raises: bool = False,
    ) -> None:
        self._picked = picked
        self._probability = probability
        self._outcome = outcome
        self._raises = raises
        self.calls: list[dict[str, object]] = []

    async def decide[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        decision_id: str,
        decision_version: int,
        output_type: type[OutputT],
        state: str,
    ) -> DecisionResult[OutputT]:
        self.calls.append(
            {
                "user_id": user_id,
                "decision_id": decision_id,
                "decision_version": decision_version,
                "state": state,
            }
        )
        if self._raises:
            raise RuntimeError("the row write failed")
        if self._outcome is not LlmCallOutcome.SUCCESS or self._picked is None:
            return DecisionResult(output=None, outcome=self._outcome)
        return DecisionResult(
            output=output_type.model_validate({"kind": self._picked}),
            outcome=self._outcome,
            probabilities={"kind": {self._picked: self._probability}},
        )


def _kinds(decisions: _Decisions) -> JevGoalKind:
    return JevGoalKind(decisions, threshold=_THRESHOLD)


@pytest.mark.parametrize(
    ("goal", "kind"),
    [
        ("Touch my toes by summer", GoalKind.MOBILITY),
        ("Lose 5 kg", GoalKind.WEIGHT_LOSS),
        ("Deadlift 150 kg", GoalKind.STRENGTH),
    ],
)
async def test_a_confident_pick_is_read_by_option_name(goal: str, kind: GoalKind) -> None:
    """AC: "touch my toes by summer" → mobility, "lose 5 kg" → weight loss, "deadlift 150 kg" →
    strength, each read by option name from Jev's distribution."""
    decisions = _Decisions(picked=kind.value)

    assert await _kinds(decisions).kind_of(_USER, goal) == kind
    assert [(call["decision_id"], call["decision_version"]) for call in decisions.calls] == [
        (GOAL_KIND_ID, GOAL_KIND_VERSION)
    ]


async def test_a_pick_at_the_threshold_counts() -> None:
    decisions = _Decisions(probability=_THRESHOLD)

    assert await _kinds(decisions).kind_of(_USER, "Touch my toes") == GoalKind.MOBILITY


async def test_a_pick_below_the_threshold_is_no_kind() -> None:
    decisions = _Decisions(probability=_THRESHOLD - 0.01)

    assert await _kinds(decisions).kind_of(_USER, "Touch my toes") is None


async def test_none_is_no_kind() -> None:
    decisions = _Decisions(picked=NO_KIND, probability=0.99)

    assert await _kinds(decisions).kind_of(_USER, "Have fun") is None


@pytest.mark.parametrize(
    "outcome", [LlmCallOutcome.TIMEOUT, LlmCallOutcome.ERROR, LlmCallOutcome.RATE_LIMITED]
)
async def test_a_failed_call_is_no_kind(outcome: LlmCallOutcome) -> None:
    assert await _kinds(_Decisions(outcome=outcome)).kind_of(_USER, "Lose 5 kg") is None


async def test_a_raising_gateway_is_no_kind() -> None:
    assert await _kinds(_Decisions(raises=True)).kind_of(_USER, "Lose 5 kg") is None


async def test_the_state_is_the_goal_text_only() -> None:
    """AC: no identity in Jev's state, only the goal's text (ADR-0020 invariant 9)."""
    decisions = _Decisions()

    await _kinds(decisions).kind_of(_USER, "Touch my toes by summer")

    (call,) = decisions.calls
    assert GoalKindState.model_validate_json(str(call["state"])) == GoalKindState(
        goal="Touch my toes by summer"
    )
    assert str(_USER) not in str(call["state"])
    with pytest.raises(ValidationError):
        GoalKindState.model_validate({"goal": "Lose 5 kg", "user_id": str(_USER)})


def test_the_loaded_output_type_matches_the_file() -> None:
    """The file offers exactly the six kinds and `none`, and the loaded type takes each."""
    raw = yaml.safe_load((_DECISIONS / GOAL_KIND_ID / f"v{GOAL_KIND_VERSION}.en.yaml").read_text())
    options = set(raw["questions"]["kind"]["options"])
    output_type = goal_kind_set().output_type

    assert options == {kind.value for kind in GoalKind} | {NO_KIND}
    for option in options:
        assert output_type.model_validate({"kind": option}).model_dump() == {"kind": option}
    with pytest.raises(ValidationError):
        output_type.model_validate({"kind": "body_composition"})


def test_the_threshold_starts_high() -> None:
    """A wrong kind is seen and fixed on the summary, a missing one costs an empty pick, so a
    new set starts on the side of missing (ADR-0034)."""
    assert Settings.model_fields["goal_kind_threshold"].default == _THRESHOLD
