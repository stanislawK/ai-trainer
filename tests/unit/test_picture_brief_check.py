"""`JevPictureBriefCheck` (ADR-0031 *Picture on the card*, #642): Jev reads the catalogue
instructions and the picture brief, and the brief passes only when both questions are at or above
their own threshold from `Settings`. A timeout, an error, a missing output or a raising gateway
fails closed; a budget refusal is its own result, since no check ran. The state is the
instructions and the brief only."""

import json
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from ai_trainer.application.ports.decision_gateway import DecisionResult
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.media_generation import BriefCheck, BriefCheckRefused
from ai_trainer.llm.picture_brief_check import (
    PICTURE_BRIEF_CHECK_ID,
    PICTURE_BRIEF_CHECK_QUESTIONS,
    PICTURE_BRIEF_CHECK_VERSION,
    BriefCheckState,
    BriefCheckThresholds,
    JevPictureBriefCheck,
    picture_brief_check_set,
)

_USER = uuid4()
_MOVEMENT = 0.5
_RULES = 0.5
_THRESHOLDS = BriefCheckThresholds(same_movement=_MOVEMENT, keeps_rules=_RULES)
_INSTRUCTIONS = "Hang from a pull-up bar with your arms fully extended, then pull your chest up."
_BRIEF = "One person hanging from a bar, then chest at the bar. Plain background. No text."


class _Decisions:
    """Implements `DecisionGatewayPort`: answers both questions with the given P(yes)."""

    def __init__(
        self,
        same_movement: float = 0.95,
        keeps_rules: float = 0.95,
        *,
        outcome: LlmCallOutcome = LlmCallOutcome.SUCCESS,
        raises: bool = False,
        no_output: bool = False,
    ) -> None:
        self._answers = {"same_movement": same_movement, "keeps_rules": keeps_rules}
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


async def _check(decisions: _Decisions) -> BriefCheck | BriefCheckRefused:
    return await JevPictureBriefCheck(decisions, thresholds=_THRESHOLDS).check(
        user_id=_USER, instructions=_INSTRUCTIONS, brief=_BRIEF
    )


_FAILED = BriefCheck(passed=False, version=PICTURE_BRIEF_CHECK_VERSION)
_PASSED = BriefCheck(passed=True, version=PICTURE_BRIEF_CHECK_VERSION)


def test_output_type_matches_the_file() -> None:
    schema = picture_brief_check_set().output_type.model_json_schema()["properties"]

    assert tuple(schema) == PICTURE_BRIEF_CHECK_QUESTIONS == ("same_movement", "keeps_rules")
    for question in schema.values():
        assert (question["type"], question["minimum"], question["maximum"]) == ("number", 0, 1)


async def test_state_carries_only_instructions_and_brief() -> None:
    decisions = _Decisions()

    await _check(decisions)

    [call] = decisions.calls
    assert call["user_id"] == _USER
    assert (call["decision_id"], call["decision_version"]) == (
        PICTURE_BRIEF_CHECK_ID,
        PICTURE_BRIEF_CHECK_VERSION,
    )
    assert isinstance(call["state"], str)
    assert json.loads(call["state"]) == {"instructions": _INSTRUCTIONS, "brief": _BRIEF}
    assert str(_USER) not in call["state"]


def test_state_refuses_any_other_field() -> None:
    with pytest.raises(ValidationError):
        BriefCheckState.model_validate({"instructions": "a", "brief": "b", "user_id": str(_USER)})


# AC 1: a good brief passes.
async def test_both_questions_at_threshold_pass() -> None:
    assert await _check(_Decisions(_MOVEMENT, _RULES)) == _PASSED


# AC 2: a brief for another movement fails.
async def test_movement_below_threshold_fails() -> None:
    assert await _check(_Decisions(_MOVEMENT - 0.01, 0.99)) == _FAILED


# AC 3: a brief asking for labels fails.
async def test_rules_below_threshold_fails() -> None:
    assert await _check(_Decisions(0.99, _RULES - 0.01)) == _FAILED


async def test_each_question_is_read_at_its_own_threshold() -> None:
    check = JevPictureBriefCheck(
        _Decisions(0.3, 0.3), thresholds=BriefCheckThresholds(same_movement=0.2, keeps_rules=0.4)
    )

    result = await check.check(user_id=_USER, instructions=_INSTRUCTIONS, brief=_BRIEF)

    assert result == _FAILED
    passing = JevPictureBriefCheck(
        _Decisions(0.3, 0.3), thresholds=BriefCheckThresholds(same_movement=0.2, keeps_rules=0.3)
    )
    assert await passing.check(user_id=_USER, instructions=_INSTRUCTIONS, brief=_BRIEF) == _PASSED


# AC 4: a Jev timeout fails closed.
async def test_timeout_fails_closed() -> None:
    assert await _check(_Decisions(outcome=LlmCallOutcome.TIMEOUT)) == _FAILED


@pytest.mark.parametrize(
    "decisions",
    [
        _Decisions(outcome=LlmCallOutcome.ERROR),
        _Decisions(no_output=True),
        _Decisions(raises=True),
    ],
    ids=["error", "no_output", "raises"],
)
async def test_error_and_exception_fail_closed(decisions: _Decisions) -> None:
    assert await _check(decisions) == _FAILED


async def test_rate_limited_is_refused() -> None:
    """For the card path's budget (#643): the budget refused it, so no check ran."""
    assert await _check(_Decisions(outcome=LlmCallOutcome.RATE_LIMITED)) == BriefCheckRefused()
