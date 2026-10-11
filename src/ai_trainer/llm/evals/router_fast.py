"""`router_fast`'s eval (ADR-0009 amendment, ADR-0020, #122). The set has no dataset of its own:
it runs on the router's cases (`evals/datasets/router.yaml`), whose labels already say what a
right route is. Each case is asked with that case's own sports, routed by the app's own
`fast_intents` at the `Settings` thresholds, and a case it takes is graded with the router's own
matching (`kinds_match`, `sport_hits`).

- `fast_route_safe` (assertion): the case was declined, or taken and routed right.
- `coverage` (score): the share of cases taken.
- `accuracy`: the share of taken cases routed right. The run refuses a baseline when it is
  below the router's committed pass rate, the LLM router's accuracy on the same cases.
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
    fast_intents,
    router_fast_set,
)

_SAFE = "fast_route_safe"
_COVERAGE = "coverage"
_ACCURACY = "accuracy"
_SPORT_INTENT = "log_session"


class FastRouteEvalOutput(DecisionEvalOutput):
    """Jev's answers and the numbers behind them, plus the route the app would take: `None`
    when the case goes to the LLM router."""

    intents: list[Intent] | None


class FastPathAccuracyError(RuntimeError):
    """The fast path is not as accurate as the LLM router on the cases it takes, or there is
    nothing to compare: no case taken, or no router baseline."""


# A router case's YAML, as Pydantic Evals loads it into a `Dataset[..., object, object]`.
_CASE_YAML = TypeAdapter(dict[str, Any])


@dataclass
class FastRouteGrade(Evaluator[EvalCaseInputs, object, object]):
    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, object, object]) -> dict[str, Any]:
        assert isinstance(ctx.output, FastRouteEvalOutput)
        if ctx.output.intents is None:
            return {_SAFE: True, _COVERAGE: 0.0}
        expected = _CASE_YAML.validate_python(ctx.expected_output)["intents"]
        right = _routed_right(
            ctx.output.intents, expected, metadata=_CASE_YAML.validate_python(ctx.metadata or {})
        )
        return {_SAFE: right, _COVERAGE: 1.0}


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
) -> RouterFastEvalRunResult:
    """Writes `evals/reports/router_fast.json` and, when the fast path is at least as accurate
    as the router's baseline, `evals/baselines/router_fast.json`. No `llm_calls` row: no user
    is acting, and the dataset holds no athlete text."""
    router_baseline = load_baseline(baselines_root / f"{ROUTER_TEMPLATE_ID}.json")
    if router_baseline is None:
        raise FastPathAccuracyError("There is no router baseline to compare the fast path with")
    agent: Agent[None, Any] = Agent(model)

    async def task(inputs: EvalCaseInputs) -> FastRouteEvalOutput:
        sports = [str(sport) for sport in inputs.deps.get("athlete_sports", [])]  # type: ignore[attr-defined]
        decision_set = router_fast_set(sports, loader=loader)
        result = await agent.run(inputs.prompt, output_type=decision_set.output_type)
        distributions: dict[str, dict[str, float]] = (result.response.provider_details or {}).get(
            "probabilities", {}
        )
        answers = result.output.model_dump(mode="json")
        return FastRouteEvalOutput(
            answers=answers,
            probabilities={
                name: value_behind(answer, distributions.get(name), None)
                for name, answer in answers.items()
            },
            intents=fast_intents(
                result.output, distributions, athlete_sports=sports, thresholds=thresholds
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
    }
    print(
        f"graded at thresholds: multi_request <= {thresholds.multi_request:g}, "
        f"intent >= {thresholds.intent:g}, sport >= {thresholds.sport:g}"
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
            "scores": {_COVERAGE: coverage, _ACCURACY: accuracy},
            "decision_thresholds": as_dict,
            "cases": probabilities,
        }
    )
    previous, regressions = record_baseline(baselines_root, current)
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
