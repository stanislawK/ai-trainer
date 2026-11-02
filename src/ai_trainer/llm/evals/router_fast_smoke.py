"""The `router_fast` smoke set (ADR-0034, ticket #436): `evals/smoke/router_fast.yaml` holds
messages with the athlete's sports and a label: the intents the fast path should take, or `null`
when the LLM router should decide. Each case is asked of Jev once and graded on the route the app
would take at the `Settings` thresholds, through the app's own `fast_route`. No judge and no
`llm_calls` row: no user is acting, and the cases hold no athlete identity."""

from collections.abc import Mapping

from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent
from pydantic_ai.models import Model

from ai_trainer.domain.conversation import Intent
from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.evals.smoke import SmokeCaseResult, SmokeError, SmokeSet
from ai_trainer.llm.router_fast import RouterFastThresholds, fast_route, router_fast_set


class _Deps(BaseModel):
    athlete_sports: list[str]


class _Expected(BaseModel):
    # Required: `null` is a label, the LLM router's turn, not a missing one.
    intents: list[Intent] | None


class _Case(BaseModel):
    name: str
    inputs: EvalCaseInputs
    expected_output: _Expected


def _read(smoke_set: SmokeSet) -> list[tuple[_Case, list[str]]]:
    """Every case checked before any call, so a typo in a label costs nothing."""
    raw_cases = smoke_set.dataset["cases"]
    assert isinstance(raw_cases, list)
    read: list[tuple[_Case, list[str]]] = []
    for raw in raw_cases:
        try:
            case = _Case.model_validate(raw)
            deps = _Deps.model_validate(case.inputs.deps)
        except ValidationError as error:
            raise SmokeError(f"{smoke_set.id}: {error}") from error
        read.append((case, deps.athlete_sports))
    return read


async def run_router_fast_smoke(
    smoke_set: SmokeSet,
    model: Model,
    *,
    thresholds: RouterFastThresholds,
) -> list[SmokeCaseResult]:
    """Asks each case once, in order. A case whose call fails fails alone."""
    results: list[SmokeCaseResult] = []
    for case, sports in _read(smoke_set):
        try:
            got = await _route(case, sports, model, thresholds=thresholds)
        except Exception as error:
            results.append(_result(smoke_set, case.name, False, f"the call failed: {error}"))
            continue
        wanted = case.expected_output.intents
        detail = (
            "" if got == wanted else f"wanted {_named(wanted)}, the app would take {_named(got)}"
        )
        results.append(_result(smoke_set, case.name, got == wanted, detail))
    return results


async def _route(
    case: _Case,
    sports: list[str],
    model: Model,
    *,
    thresholds: RouterFastThresholds,
) -> list[Intent] | None:
    """The intents the fast path would dispatch, or `None` when the LLM router must decide."""
    decision_set = router_fast_set(sports, loader=DecisionSetLoader())
    agent: Agent[None, BaseModel] = Agent(model, output_type=decision_set.output_type)
    result = await agent.run(case.inputs.prompt)
    distributions: Mapping[str, Mapping[str, float]] = (result.response.provider_details or {}).get(
        "probabilities", {}
    )
    route = fast_route(result.output, distributions, athlete_sports=sports, thresholds=thresholds)
    return None if route.intents is None else list(route.intents)


def _named(intents: list[Intent] | None) -> str:
    if intents is None:
        return "the LLM router"
    return ", ".join(
        intent.kind if intent.sport is None else f"{intent.kind} ({intent.sport})"
        for intent in intents
    )


def _result(smoke_set: SmokeSet, name: str, passed: bool, detail: str) -> SmokeCaseResult:
    return SmokeCaseResult(
        name=name, passed=passed, detail=detail, gap=smoke_set.known_gaps.get(name)
    )
