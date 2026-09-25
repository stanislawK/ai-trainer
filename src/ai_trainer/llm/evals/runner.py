from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai.models import Model
from pydantic_evals import Dataset
from pydantic_evals.evaluators import Evaluator
from pydantic_evals.evaluators.llm_as_a_judge import set_default_judge_model
from pydantic_evals.reporting import EvaluationReport, ReportCaseAggregate

from ai_trainer.llm.evals.baseline import (
    Baseline,
    BaselineThresholds,
    find_regressions,
    load_baseline,
    save_baseline,
)
from ai_trainer.llm.prompts.registry import PromptRegistry


class EvalCaseInputs(BaseModel):
    """A dataset case's input: the user-facing prompt plus the template's deps as a plain
    dict, so one `Dataset[EvalCaseInputs, ...]` shape works for every template (ADR-0009)
    without a per-template generated Inputs type."""

    prompt: str
    deps: dict[str, object] = {}


class EvalCaseSummary(BaseModel):
    name: str
    scores: dict[str, float]
    assertions: dict[str, bool]
    evaluator_failures: list[str]


class EvalReportSummary(BaseModel):
    """Written to `evals/reports/<template_id>.json` (gitignored, ADR-0009)."""

    name: str
    cases: list[EvalCaseSummary]
    failures: list[str]
    averages: ReportCaseAggregate | None


class JudgeModelMatchesModelUnderTestError(ValueError):
    """The judge model must differ from the model under test (ADR-0009 invariant 3)."""

    def __init__(self, model_id: str) -> None:
        super().__init__(
            f"The judge model must differ from the model under test; both are {model_id!r}"
        )
        self.model_id = model_id


class EvaluatorFailedError(RuntimeError):
    """An evaluator raised while grading a case (e.g. a schema mismatch or a judge-model
    error) — a baseline computed from these results would silently understate the failure,
    so `run_eval` refuses to write one and surfaces it as loudly as a task-level error."""

    def __init__(self, failures: list[str]) -> None:
        super().__init__("evaluator(s) failed: " + "; ".join(failures))
        self.failures = failures


@dataclass(frozen=True, slots=True)
class EvalRunResult:
    report: EvaluationReport[EvalCaseInputs, BaseModel, object]
    baseline: Baseline
    previous_baseline: Baseline | None
    regressions: list[str]


def summarize_report(
    report: EvaluationReport[EvalCaseInputs, BaseModel, object],
) -> EvalReportSummary:
    return EvalReportSummary(
        name=report.name,
        cases=[
            EvalCaseSummary(
                name=case.name,
                scores={name: float(result.value) for name, result in case.scores.items()},
                assertions={name: bool(result.value) for name, result in case.assertions.items()},
                evaluator_failures=[
                    f"{failure.name}: {failure.error_message}"
                    for failure in case.evaluator_failures
                ],
            )
            for case in report.cases
        ],
        failures=[failure.name for failure in report.failures],
        averages=report.averages(),
    )


def _evaluator_failures(
    report: EvaluationReport[EvalCaseInputs, BaseModel, object],
) -> list[str]:
    return [
        f"{case.name} / {failure.name}: {failure.error_message}"
        for case in report.cases
        for failure in case.evaluator_failures
    ]


async def run_eval(
    *,
    registry: PromptRegistry,
    template_id: str,
    version: int,
    model: Model,
    model_id: str,
    judge_model: Model,
    judge_model_id: str,
    datasets_root: Path,
    baselines_root: Path,
    reports_root: Path,
    custom_evaluator_types: Sequence[type[Evaluator[EvalCaseInputs, BaseModel, object]]] = (),
) -> EvalRunResult:
    """Runs `template_id`'s dataset against `model`, writes a report and a baseline, and
    reports any regression against the previously committed baseline (ADR-0009).

    `model` and `judge_model` are already-resolved `Model` instances so the caller (the CLI)
    decides whether they're real `OpenRouterModel`s or, in a test, a scripted `FunctionModel`
    — this function never reaches a real model itself. `custom_evaluator_types` lets a
    template's dataset reference custom `Evaluator` subclasses beyond Pydantic Evals' built-ins
    (e.g. `LLMJudge`), since the dataset YAML only stores evaluator names.
    """
    if judge_model_id == model_id:
        raise JudgeModelMatchesModelUnderTestError(model_id)

    set_default_judge_model(judge_model)

    template = registry.get(template_id, version)
    agent = registry.build_agent(template_id, version)

    async def task(inputs: EvalCaseInputs) -> BaseModel:
        deps = template.deps_type.model_validate(inputs.deps)
        result = await agent.run(inputs.prompt, deps=deps, model=model)
        return result.output

    # Subscripted with `template.output_type` (the template's real, runtime output model), not
    # the `BaseModel` this function is statically typed against: Pydantic Evals deserializes
    # `Case.expected_output` and validates it against this generic parameter, and built-ins
    # like `EqualsExpected` compare real model instances — subscripting with bare `BaseModel`
    # would build an uninstantiable `expected_output` and crash every evaluator that touches
    # it, silently, since a crashed evaluator drops out of `report.averages()` rather than
    # raising (caught below via `_evaluator_failures`).
    # mypy can't express a generic parameter only known at runtime (there's no static type
    # for "whatever this template's output_type happens to be") — `output_type` is a real
    # class object at runtime, which is all `Dataset.__class_getitem__` needs.
    output_type = template.output_type
    dataset: Dataset[EvalCaseInputs, BaseModel, object] = Dataset[
        EvalCaseInputs, output_type, object  # type: ignore[valid-type]
    ].from_file(
        datasets_root / f"{template_id}.yaml",
        custom_evaluator_types=custom_evaluator_types,
    )

    report = await dataset.evaluate(task, name=f"{template_id} v{version} ({model_id})")
    report.print(include_input=True, include_output=True)

    reports_root.mkdir(parents=True, exist_ok=True)
    summary = summarize_report(report)
    (reports_root / f"{template_id}.json").write_text(summary.model_dump_json(indent=2) + "\n")

    if evaluator_failures := _evaluator_failures(report):
        raise EvaluatorFailedError(evaluator_failures)

    averages = report.averages()
    current = Baseline(
        template_id=template_id,
        version=version,
        model=model_id,
        # `None` here means no evaluator produced an assertion at all (a dataset with only
        # score evaluators) — a legitimate case now that a crashed evaluator raises above
        # instead of silently leaving `averages.assertions` empty. Treated as a clean pass.
        assertions=(
            averages.assertions if averages is not None and averages.assertions is not None else 1.0
        ),
        scores={name: float(score) for name, score in averages.scores.items()}
        if averages is not None
        else {},
    )

    baseline_path = baselines_root / f"{template_id}.json"
    previous = load_baseline(baseline_path)
    regressions = (
        find_regressions(previous=previous, current=current) if previous is not None else []
    )
    save_baseline(
        baseline_path,
        current.model_copy(
            update={
                "thresholds": previous.thresholds if previous is not None else BaselineThresholds()
            }
        ),
    )

    return EvalRunResult(
        report=report, baseline=current, previous_baseline=previous, regressions=regressions
    )
