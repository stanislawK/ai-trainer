"""The `goal_kind` smoke set (ADR-0034, #625): `evals/smoke/goal_kind.yaml` holds onboarding goals,
each labelled with its kind or `none`. Each is asked of Jev once, with the state the app builds,
and graded on what the app would store at the `Settings` threshold, through its own
`confident_kind`: an expected `none` passes when the app would store no kind. No judge and no
`llm_calls` row: no user is acting."""

from collections.abc import Mapping

from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent
from pydantic_ai.models import Model

from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.smoke import SmokeCaseResult, SmokeError, SmokeSet
from ai_trainer.llm.goal_kind import NO_KIND, GoalKindState, confident_kind, goal_kind_set


class _Inputs(BaseModel):
    prompt: str
    deps: GoalKindState


class _Expected(BaseModel):
    kind: str


class _Case(BaseModel):
    name: str
    inputs: _Inputs
    expected_output: _Expected


def _read(smoke_set: SmokeSet) -> list[_Case]:
    """Every case checked before any call, so a typo in a label costs nothing."""
    raw_cases = smoke_set.dataset["cases"]
    assert isinstance(raw_cases, list)
    try:
        return [_Case.model_validate(raw) for raw in raw_cases]
    except ValidationError as error:
        raise SmokeError(f"{smoke_set.id}: {error}") from error


async def run_goal_kind_smoke(
    smoke_set: SmokeSet,
    model: Model,
    *,
    threshold: float,
    loader: DecisionSetLoader | None = None,
) -> list[SmokeCaseResult]:
    """Asks each case once, in order. A case whose call fails fails alone."""
    decision_set = goal_kind_set(loader)
    agent: Agent[None, BaseModel] = Agent(model, output_type=decision_set.output_type)
    results: list[SmokeCaseResult] = []
    for case in _read(smoke_set):
        try:
            result = await agent.run(case.inputs.deps.model_dump_json())
        except Exception as error:
            results.append(_result(smoke_set, case.name, f"the call failed: {error}"))
            continue
        distributions: Mapping[str, Mapping[str, float]] = (
            result.response.provider_details or {}
        ).get("probabilities", {})
        kind = confident_kind(result.output, distributions, threshold=threshold)
        got = kind.value if kind is not None else NO_KIND
        wanted = case.expected_output.kind
        detail = "" if got == wanted else f"wanted {wanted!r}, the app would store {got!r}"
        results.append(_result(smoke_set, case.name, detail))
    return results


def _result(smoke_set: SmokeSet, name: str, detail: str) -> SmokeCaseResult:
    return SmokeCaseResult(
        name=name, passed=not detail, detail=detail, gap=smoke_set.known_gaps.get(name)
    )
