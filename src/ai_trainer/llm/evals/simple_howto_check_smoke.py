"""The `exercise_simple_howto_check` smoke set (ADR-0034, #591):
`evals/smoke/exercise_simple_howto_check.yaml` holds catalogue instructions and beginner
summaries, each labelled with whether the app should store it. Each is asked of Jev once, with the
state the app builds, and graded at each question's own `Settings` threshold through the app's own
`check_passes`. No judge and no `llm_calls` row: no user is acting."""

from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent
from pydantic_ai.models import Model

from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.smoke import SmokeCaseResult, SmokeError, SmokeSet
from ai_trainer.llm.simple_howto_check import (
    HowtoCheckState,
    HowtoCheckThresholds,
    check_passes,
    simple_howto_check_set,
)


class _Inputs(BaseModel):
    prompt: str
    deps: HowtoCheckState


class _Expected(BaseModel):
    passes: bool


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


async def run_simple_howto_check_smoke(
    smoke_set: SmokeSet,
    model: Model,
    *,
    thresholds: HowtoCheckThresholds,
    loader: DecisionSetLoader | None = None,
) -> list[SmokeCaseResult]:
    """Asks each case once, in order. A case whose call fails fails alone."""
    decision_set = simple_howto_check_set(loader)
    agent: Agent[None, BaseModel] = Agent(model, output_type=decision_set.output_type)
    results: list[SmokeCaseResult] = []
    for case in _read(smoke_set):
        try:
            result = await agent.run(case.inputs.deps.model_dump_json())
        except Exception as error:
            results.append(_result(smoke_set, case.name, f"the call failed: {error}"))
            continue
        got = check_passes(result.output, thresholds=thresholds)
        wanted = case.expected_output.passes
        detail = (
            ""
            if got == wanted
            else f"wanted passes={wanted}, the app would read passes={got} "
            f"({result.output.model_dump_json()})"
        )
        results.append(_result(smoke_set, case.name, detail))
    return results


def _result(smoke_set: SmokeSet, name: str, detail: str) -> SmokeCaseResult:
    return SmokeCaseResult(
        name=name, passed=not detail, detail=detail, gap=smoke_set.known_gaps.get(name)
    )
