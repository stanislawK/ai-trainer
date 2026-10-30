"""`router_fast`'s eval (ADR-0009 amendment, ADR-0020, #122). The set has no dataset of its own:
it runs on the router's cases (`evals/datasets/router.yaml`), whose labels already say what a
right route is. Each case is asked with that case's own sports, routed by the app's own
`fast_intents` at the `Settings` thresholds, and a case it takes is graded with the router's own
matching (`kinds_match`, `sport_hits`).

- `fast_route_safe` (assertion): the case was declined, or taken and routed right.
- `coverage` (score): the share of cases taken.
- `accuracy`: the share of taken cases routed right. The run refuses a baseline when it is
  below the router's committed pass rate, the LLM router's accuracy on the same cases.
- `plausible_sports_right` (assertion) and `plausible_accuracy` (score), on the cases that list
  `metadata.plausible_sports` (B23, #123): the sports Jev leaves in play, compared as a set,
  whether or not the case was taken. The score is a regression-checked baseline score.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_evals import Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext
from pydantic_evals.reporting import EvaluationReport

from ai_trainer.domain.conversation import Intent
from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.baseline import Baseline, find_changes, load_baseline
from ai_trainer.llm.evals.decisions import (
    DecisionEvalOutput,
    DecisionReportSummary,
    case_probabilities,
    print_spread,
    value_behind,
)
from ai_trainer.llm.evals.runner import (
    EvalCaseInputs,
    current_baseline,
    raise_on_failures,
    record_baseline,
    summarize_report,
)
from ai_trainer.llm.router import ROUTER_TEMPLATE_ID, kinds_match, sport_hits
from ai_trainer.llm.router_fast import (
    ROUTER_FAST_ID,
    RouterFastThresholds,
    fast_route,
    router_fast_set,
)

_SAFE = "fast_route_safe"
_COVERAGE = "coverage"
_ACCURACY = "accuracy"
_PLAUSIBLE_RIGHT = "plausible_sports_right"
_PLAUSIBLE_ACCURACY = "plausible_accuracy"
_SPORT_INTENT = "log_session"


class FastRouteEvalOutput(DecisionEvalOutput):
    """Jev's answers and the numbers behind them, plus the route the app would take: `None`
    when the case goes to the LLM router."""

    intents: list[Intent] | None
    plausible_sports: list[str] | None


class FastPathAccuracyError(RuntimeError):
    """The fast path is not as accurate as the LLM router on the cases it takes, or there is
    nothing to compare: no case taken, or no router baseline."""


# A router case's YAML, as Pydantic Evals loads it into a `Dataset[..., object, object]`.
_CASE_YAML = TypeAdapter(dict[str, Any])


@dataclass
class FastRouteGrade(Evaluator[EvalCaseInputs, object, object]):
    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, object, object]) -> dict[str, Any]:
        assert isinstance(ctx.output, FastRouteEvalOutput)
        metadata = _CASE_YAML.validate_python(ctx.metadata or {})
        grades: dict[str, Any] = {}
        if (wanted := metadata.get("plausible_sports")) is not None:
            grades[_PLAUSIBLE_RIGHT] = ctx.output.plausible_sports is not None and set(
                ctx.output.plausible_sports
            ) == set(wanted)
        if ctx.output.intents is None:
            return {_SAFE: True, _COVERAGE: 0.0, **grades}
        expected = _CASE_YAML.validate_python(ctx.expected_output)["intents"]
        right = _routed_right(ctx.output.intents, expected, metadata=metadata)
        return {_SAFE: right, _COVERAGE: 1.0, **grades}


def _routed_right(
    actual: list[Intent], expected: list[dict[str, Any]], *, metadata: dict[str, Any]
) -> bool:
    return (
        kinds_match(
            [intent.kind for intent in actual],
            [intent["kind"] for intent in expected],
            metadata=metadata,
        )
        and sport_hits(
            [str(intent.sport) for intent in actual if intent.kind == _SPORT_INTENT],
            [intent["sport"] for intent in expected if intent["kind"] == _SPORT_INTENT],
            metadata=metadata,
        )
        == 1.0
    )


@dataclass(frozen=True, slots=True)
class RouterFastEvalRunResult:
    report: EvaluationReport[EvalCaseInputs, object, object]
    baseline: Baseline
    previous_baseline: Baseline | None
    regressions: list[str]
    changes: list[str]
    coverage: float
    accuracy: float
    router_accuracy: float


async def run_router_fast_eval(
    *,
    loader: DecisionSetLoader,
    version: int,
    model: Model,
    model_id: str,
    thresholds: RouterFastThresholds,
    datasets_root: Path,
    baselines_root: Path,
    reports_root: Path,
    repeat: int = 1,
    promote: bool = False,
) -> RouterFastEvalRunResult:
    """Writes `evals/reports/router_fast.json` and, when `promote`d and the fast path is at least
    as accurate as the router's baseline, `evals/baselines/router_fast.json`. No `llm_calls`
    row: no user is acting, and the dataset holds no athlete text."""
    router_baseline = load_baseline(baselines_root / f"{ROUTER_TEMPLATE_ID}.json")
    if router_baseline is None:
        raise FastPathAccuracyError("There is no router baseline to compare the fast path with")
    agent: Agent[None, Any] = Agent(model)

    async def task(inputs: EvalCaseInputs) -> FastRouteEvalOutput:
        # `deps` is the case's free-form dict that Pydantic Evals types as `object`, so
        # mypy cannot see `.get` (attr-defined).
        sports = [str(sport) for sport in inputs.deps.get("athlete_sports", [])]  # type: ignore[attr-defined]
        decision_set = router_fast_set(sports, loader=loader, version=version)
        result = await agent.run(inputs.prompt, output_type=decision_set.output_type)
        distributions: dict[str, dict[str, float]] = (result.response.provider_details or {}).get(
            "probabilities", {}
        )
        answers = result.output.model_dump(mode="json")
        route = fast_route(
            result.output, distributions, athlete_sports=sports, thresholds=thresholds
        )
        return FastRouteEvalOutput(
            answers=answers,
            probabilities={
                name: value_behind(answer, distributions.get(name), None)
                for name, answer in answers.items()
            },
            intents=None if route.intents is None else list(route.intents),
            plausible_sports=(
                None if route.plausible_sports is None else list(route.plausible_sports)
            ),
        )

    data = yaml.safe_load((datasets_root / f"{ROUTER_TEMPLATE_ID}.yaml").read_text())
    data.pop("evaluators", None)  # The router's own evaluators grade the router's output.
    dataset = Dataset[EvalCaseInputs, object, object].from_dict(data)
    dataset.add_evaluator(FastRouteGrade())
    as_dict = {
        "multi_request": thresholds.multi_request,
        "intent": thresholds.intent,
        "sport": thresholds.sport,
        "plausible_floor": thresholds.plausible_floor,
    }
    print(
        f"graded at thresholds: multi_request <= {thresholds.multi_request:g}, "
        f"intent >= {thresholds.intent:g}, sport >= {thresholds.sport:g}, "
        f"plausible sport >= {thresholds.plausible_floor:g}"
    )
    report = await dataset.evaluate(
        task, name=f"{ROUTER_FAST_ID} v{version} ({model_id})", repeat=repeat
    )
    report.print(include_input=True, include_output=True)

    probabilities = case_probabilities(report)
    print_spread(probabilities)
    reports_root.mkdir(parents=True, exist_ok=True)
    summary = DecisionReportSummary(
        **summarize_report(report).model_dump(), probabilities=probabilities
    )
    (reports_root / f"{ROUTER_FAST_ID}.json").write_text(summary.model_dump_json(indent=2) + "\n")

    raise_on_failures(report)
    coverage, accuracy = _coverage_and_accuracy(report)
    print(
        f"coverage {coverage:.3f}, accuracy on taken cases {accuracy:.3f}, "
        f"router baseline {router_baseline.assertions:.3f}"
    )
    if accuracy < router_baseline.assertions:
        raise FastPathAccuracyError(
            f"Fast-path accuracy {accuracy:.3f} is below the router's "
            f"{router_baseline.assertions:.3f}; no baseline written."
        )
    current = current_baseline(
        report, template_id=ROUTER_FAST_ID, version=version, model_id=model_id
    ).model_copy(
        update={
            "scores": {_COVERAGE: coverage, _ACCURACY: accuracy, **_plausible_score(report)},
            "decision_thresholds": as_dict,
            "cases": probabilities,
        }
    )
    previous, regressions = record_baseline(baselines_root, current, promote=promote)
    return RouterFastEvalRunResult(
        report=report,
        baseline=current,
        previous_baseline=previous,
        regressions=regressions,
        changes=find_changes(previous=previous, current=current) if previous is not None else [],
        coverage=coverage,
        accuracy=accuracy,
        router_accuracy=router_baseline.assertions,
    )


def _coverage_and_accuracy(
    report: EvaluationReport[EvalCaseInputs, object, object],
) -> tuple[float, float]:
    """Over every run of every case, repeats included."""
    taken = [case for case in report.cases if case.scores[_COVERAGE].value == 1.0]
    if not taken:
        raise FastPathAccuracyError("The fast path took no case; no baseline written.")
    right = sum(1 for case in taken if case.assertions[_SAFE].value is True)
    return len(taken) / len(report.cases), right / len(taken)


def _plausible_score(
    report: EvaluationReport[EvalCaseInputs, object, object],
) -> dict[str, float]:
    """The share of graded runs whose plausible set was right; absent when no case lists one."""
    graded = [
        case.assertions[_PLAUSIBLE_RIGHT].value
        for case in report.cases
        if _PLAUSIBLE_RIGHT in case.assertions
    ]
    if not graded:
        return {}
    return {_PLAUSIBLE_ACCURACY: sum(1 for right in graded if right is True) / len(graded)}
