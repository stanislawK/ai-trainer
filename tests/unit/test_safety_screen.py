"""The Jev safety screen (ADR-0020, G3, ticket #121): `safety_screen` v1's pain question, asked
of the bare message, read as P(pain) against the calibrated threshold, and no pain whenever the
decision did not succeed."""

from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from ai_trainer.application.ports.decision_gateway import DecisionResult
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.llm.safety_screen import JevSafetyScreen


@dataclass
class _Asked:
    user_id: UUID
    decision_id: str
    decision_version: int
    output_type: type[BaseModel]
    state: str


@dataclass
class FakeDecisions:
    """Answers P(pain) with `pain`, or fails with `outcome`; records every call."""

    pain: float = 0.0
    outcome: LlmCallOutcome = LlmCallOutcome.SUCCESS
    error: Exception | None = None
    asked: list[_Asked] = field(default_factory=list)

    async def decide[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        decision_id: str,
        decision_version: int,
        output_type: type[OutputT],
        state: str,
    ) -> DecisionResult[OutputT]:
        self.asked.append(_Asked(user_id, decision_id, decision_version, output_type, state))
        if self.error is not None:
            raise self.error
        if self.outcome is not LlmCallOutcome.SUCCESS:
            return DecisionResult(output=None, outcome=self.outcome)
        return DecisionResult(output=output_type(pain=self.pain), outcome=self.outcome)


@pytest.mark.parametrize(
    ("pain", "found"), [(0.29, False), (0.3, True), (0.97, True), (0.0, False)]
)
async def test_pain_at_or_above_the_threshold_is_found(pain: float, found: bool) -> None:
    screen = JevSafetyScreen(FakeDecisions(pain=pain), threshold=0.3)

    assert await screen.finds_pain(uuid4(), "knee's a bit sore lol, anyway hi") is found


@pytest.mark.parametrize(
    "outcome", [LlmCallOutcome.TIMEOUT, LlmCallOutcome.ERROR, LlmCallOutcome.CANCELLED]
)
async def test_a_screen_that_did_not_succeed_finds_no_pain(outcome: LlmCallOutcome) -> None:
    screen = JevSafetyScreen(FakeDecisions(pain=1.0, outcome=outcome), threshold=0.3)

    assert await screen.finds_pain(uuid4(), "my knee hurts") is False


async def test_a_gateway_that_raises_finds_no_pain_rather_than_failing_the_reply() -> None:
    """E.g. the `llm_calls` row write failing after a successful call."""
    decisions = FakeDecisions(pain=1.0, error=RuntimeError("database is down"))

    assert await JevSafetyScreen(decisions, threshold=0.3).finds_pain(uuid4(), "ouch") is False


async def test_it_asks_only_the_pain_question_of_the_bare_message_for_the_acting_user() -> None:
    decisions = FakeDecisions()
    user_id = uuid4()

    await JevSafetyScreen(decisions, threshold=0.3).finds_pain(user_id, "my knee hurts")

    [asked] = decisions.asked
    assert (asked.user_id, asked.decision_id, asked.decision_version, asked.state) == (
        user_id,
        "safety_screen",
        1,
        "my knee hurts",
    )
    assert list(asked.output_type.model_fields) == ["pain"]
