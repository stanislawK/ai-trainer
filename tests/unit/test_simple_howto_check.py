"""`JevSimpleHowtoCheck` (ADR-0031 *Jev check*, #591): each question must be at or above its own
threshold from `Settings`; a timeout, an error, a missing output or a raising gateway fails closed;
a budget refusal is its own result, since no check ran. The state is the original and the summary
only."""

import json
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from ai_trainer.application.ports.decision_gateway import DecisionResult
from ai_trainer.domain.exercise_simple_texts import HowtoCheck, HowtoCheckRefused
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.llm.simple_howto_check import (
    SIMPLE_HOWTO_CHECK_ID,
    SIMPLE_HOWTO_CHECK_VERSION,
    HowtoCheckState,
    HowtoCheckThresholds,
    JevSimpleHowtoCheck,
    simple_howto_check_set,
)

_USER = uuid4()
# The calibrated defaults' shape (2026-11-22): `keeps_cues` low, `adds_nothing` at the midpoint.
_KEEPS = 0.12
_ADDS = 0.5
_THRESHOLDS = HowtoCheckThresholds(keeps_cues=_KEEPS, adds_nothing=_ADDS)
_ORIGINAL = "Lie flat on a bench. Lower the bar slowly, keeping your elbows tucked in."
_SUMMARY = "Lie on the bench and lower the bar slowly, elbows tucked in."


class _Decisions:
    """Implements `DecisionGatewayPort`: answers both questions with the given P(yes)."""

    def __init__(
        self,
        keeps_cues: float = 0.95,
        adds_nothing: float = 0.95,
        *,
        outcome: LlmCallOutcome = LlmCallOutcome.SUCCESS,
        raises: bool = False,
        no_output: bool = False,
    ) -> None:
        self._answers = {"keeps_cues": keeps_cues, "adds_nothing": adds_nothing}
        self._outcome = outcome
        self._raises = raises
        self._no_output = no_output
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
        if self._outcome is not LlmCallOutcome.SUCCESS or self._no_output:
            return DecisionResult(output=None, outcome=self._outcome)
        return DecisionResult(
            output=output_type.model_validate(self._answers), outcome=self._outcome
        )


async def _check(decisions: _Decisions) -> HowtoCheck | HowtoCheckRefused:
    return await JevSimpleHowtoCheck(decisions, thresholds=_THRESHOLDS).check(
        user_id=_USER, original=_ORIGINAL, summary=_SUMMARY
    )


_FAILED = HowtoCheck(passed=False, version=SIMPLE_HOWTO_CHECK_VERSION)
_PASSED = HowtoCheck(passed=True, version=SIMPLE_HOWTO_CHECK_VERSION)


def test_set_loads_two_probability_questions() -> None:
    schema = simple_howto_check_set().output_type.model_json_schema()["properties"]

    assert list(schema) == ["keeps_cues", "adds_nothing"]
    for question in schema.values():
        assert (question["type"], question["minimum"], question["maximum"]) == ("number", 0, 1)


async def test_state_is_original_and_summary_only() -> None:
    decisions = _Decisions()

    await _check(decisions)

    [call] = decisions.calls
    assert call["user_id"] == _USER
    assert (call["decision_id"], call["decision_version"]) == (
        SIMPLE_HOWTO_CHECK_ID,
        SIMPLE_HOWTO_CHECK_VERSION,
    )
    assert isinstance(call["state"], str)
    assert json.loads(call["state"]) == {"original": _ORIGINAL, "summary": _SUMMARY}
    assert str(_USER) not in call["state"]


def test_state_refuses_any_other_field() -> None:
    with pytest.raises(ValidationError):
        HowtoCheckState.model_validate({"original": "a", "summary": "b", "user_id": str(_USER)})


# AC 3: at each question's own threshold passes; below its own on either fails.
async def test_at_both_thresholds_passes() -> None:
    assert await _check(_Decisions(_KEEPS, _ADDS)) == _PASSED


@pytest.mark.parametrize(
    ("keeps_cues", "adds_nothing"),
    [(_KEEPS - 0.01, 0.99), (0.99, _ADDS - 0.01), (0.01, 0.01)],
    ids=["drops_a_cue", "adds_something", "both"],
)
async def test_below_its_own_threshold_on_either_question_fails(
    keeps_cues: float, adds_nothing: float
) -> None:
    assert await _check(_Decisions(keeps_cues, adds_nothing)) == _FAILED


async def test_each_question_is_read_at_its_own_threshold() -> None:
    """0.3 is above `keeps_cues`'s threshold but below `adds_nothing`'s: it passes as one and
    fails as the other, so neither question borrows the other's threshold."""
    assert await _check(_Decisions(0.3, _ADDS)) == _PASSED
    assert await _check(_Decisions(_KEEPS, 0.3)) == _FAILED


# AC 3: a Jev timeout or error counts as a fail.
async def test_timeout_fails() -> None:
    assert await _check(_Decisions(outcome=LlmCallOutcome.TIMEOUT)) == _FAILED


async def test_error_fails() -> None:
    assert await _check(_Decisions(outcome=LlmCallOutcome.ERROR)) == _FAILED


async def test_missing_output_fails() -> None:
    assert await _check(_Decisions(no_output=True)) == _FAILED


async def test_raising_gateway_fails() -> None:
    assert await _check(_Decisions(raises=True)) == _FAILED


async def test_budget_refusal_is_not_a_check() -> None:
    """Owner's plan-gate decision (#591): the budget refused it, so no check ran and nothing is
    counted."""
    assert await _check(_Decisions(outcome=LlmCallOutcome.RATE_LIMITED)) == HowtoCheckRefused()
