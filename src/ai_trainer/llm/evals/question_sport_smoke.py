"""The `question_sport` smoke set (ADR-0034, ticket #421): `evals/smoke/question_sport.yaml` holds
training questions, each labelled with its sport or `none`. Each is asked of Jev once and graded
on what the app would read at the `Settings` threshold, through the app's own `confident_sport`.
No judge and no `llm_calls` row: no user is acting."""

from collections.abc import Mapping

from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent
from pydantic_ai.models import Model

from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.evals.smoke import SmokeCaseResult, SmokeError, SmokeSet
from ai_trainer.llm.question_sport import NO_SPORT, confident_sport, question_sport_set


class _Expected(BaseModel):
    sport: str


class _Case(BaseModel):
    name: str
    inputs: EvalCaseInputs
    expected_output: _Expected


def _read(smoke_set: SmokeSet) -> list[_Case]:
    """Every case checked before any call, so a typo in a label costs nothing."""
    raw_cases = smoke_set.dataset["cases"]
    assert isinstance(raw_cases, list)
    try:
        return [_Case.model_validate(raw) for raw in raw_cases]
    except ValidationError as error:
        raise SmokeError(f"{smoke_set.id}: {error}") from error


async def run_question_sport_smoke(
    smoke_set: SmokeSet,
    model: Model,
    *,
    threshold: float,
    loader: DecisionSetLoader | None = None,
) -> list[SmokeCaseResult]:
    """Asks each case once, in order. A case whose call fails fails alone."""
    decision_set = question_sport_set(loader)
    agent: Agent[None, BaseModel] = Agent(model, output_type=decision_set.output_type)
    results: list[SmokeCaseResult] = []
    for case in _read(smoke_set):
        try:
            result = await agent.run(case.inputs.prompt)
        except Exception as error:
            results.append(_result(smoke_set, case.name, f"the call failed: {error}"))
            continue
        distributions: Mapping[str, Mapping[str, float]] = (
            result.response.provider_details or {}
        ).get("probabilities", {})
        got = confident_sport(result.output, distributions, threshold=threshold) or NO_SPORT
        wanted = case.expected_output.sport
        detail = "" if got == wanted else f"wanted {wanted!r}, the app would read {got!r}"
        results.append(_result(smoke_set, case.name, detail))
    return results


def _result(smoke_set: SmokeSet, name: str, detail: str) -> SmokeCaseResult:
    return SmokeCaseResult(
        name=name, passed=not detail, detail=detail, gap=smoke_set.known_gaps.get(name)
    )
