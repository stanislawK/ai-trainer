import operator
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import reduce
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
from ai_trainer.llm.evals.inputs import EvalCaseInputs as EvalCaseInputs
from ai_trainer.llm.evals.jev_check import (
    JevCheckRow,
    agreement,
    agreement_by_rubric,
    errored,
    jev_check_rows,
    print_jev_checks,
)
from ai_trainer.llm.prompts.registry import PromptRegistry, output_types
from ai_trainer.llm.prompts.template import PromptTemplate, TemplateOutput

_JEV_FIELDS = {"jev_checks", "jev_agreement", "jev_agreement_by_rubric", "jev_errored"}


class EvalCaseSummary(BaseModel):
    name: str
    scores: dict[str, float]
    assertions: dict[str, bool]
    evaluator_failures: list[str]


class EvalReportSummary(BaseModel):
    """Written to `evals/reports/<template_id>.json` (gitignored, ADR-0009). A dataset with
    `JevCheck`s adds each check next to its judge, and their agreement."""

    name: str
    cases: list[EvalCaseSummary]
    failures: list[str]
    averages: ReportCaseAggregate | None
    jev_checks: list[JevCheckRow] | None = None
    jev_agreement: float | None = None
    jev_agreement_by_rubric: dict[str, float | None] | None = None
    jev_errored: int | None = None


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


class CaseFailedError(RuntimeError):
    """A case's task raised (a 401 from a wrong key, a timeout). A failed case drops
    out of `report.averages()`, so a baseline computed from the rest would overstate the pass
    rate (all failed gave `assertions: 1.0`, found at #109), and `run_eval` refuses to write
    one."""

    def __init__(self, *, failed: int, total: int, failures: list[str], first_error: str) -> None:
        super().__init__(
            f"{failed} of {total} cases failed; no baseline written. "
            f"First: {failures[0]}: {first_error}"
        )
        self.failures = failures


@dataclass(frozen=True, slots=True)
class EvalRunResult:
    report: EvaluationReport[EvalCaseInputs, TemplateOutput, object]
    baseline: Baseline
    previous_baseline: Baseline | None
    regressions: list[str]


def summarize_report[OutputT](
    report: EvaluationReport[EvalCaseInputs, OutputT, object],
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


def _evaluator_failures[OutputT](
    report: EvaluationReport[EvalCaseInputs, OutputT, object],
) -> list[str]:
    return [
        f"{case.name} / {failure.name}: {failure.error_message}"
        for case in report.cases
        for failure in case.evaluator_failures
    ]


def raise_on_failures[OutputT](report: EvaluationReport[EvalCaseInputs, OutputT, object]) -> None:
    """Refuses a baseline built from a run where a case or an evaluator crashed (#109)."""
    if report.failures:
        raise CaseFailedError(
            failed=len(report.failures),
            total=len(report.failures) + len(report.cases),
            failures=[failure.name for failure in report.failures],
            first_error=report.failures[0].error_message,
        )
    if evaluator_failures := _evaluator_failures(report):
        raise EvaluatorFailedError(evaluator_failures)


def current_baseline[OutputT](
    report: EvaluationReport[EvalCaseInputs, OutputT, object],
    *,
    template_id: str,
    version: int,
    model_id: str,
) -> Baseline:
    averages = report.averages()
    return Baseline(
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


def record_baseline(
    baselines_root: Path, current: Baseline, *, promote: bool
) -> tuple[Baseline | None, list[str]]:
    """Diffs `current` against the committed baseline and, only when `promote`d, saves it with
    the committed file's regression thresholds, regressions included: the owner approves a
    promote at the `/tune-prompt` gate (#208). Returns the previous baseline and the
    regressions."""
    baseline_path = baselines_root / f"{current.template_id}.json"
    previous = load_baseline(baseline_path)
    regressions = (
        find_regressions(previous=previous, current=current) if previous is not None else []
    )
    if not promote:
        return previous, regressions
    save_baseline(
        baseline_path,
        current.model_copy(
            update={
                "thresholds": previous.thresholds if previous is not None else BaselineThresholds()
            }
        ),
    )
    return previous, regressions


def eval_task(
    registry: PromptRegistry, template_id: str, version: int, *, model: Model
) -> Callable[[EvalCaseInputs], Awaitable[TemplateOutput]]:
    """The task a case runs: the template's agent on `model`, with deps built from the case."""
    template = registry.get(template_id, version)
    agent = registry.build_agent(template_id, version)

    async def task(inputs: EvalCaseInputs) -> TemplateOutput:
        deps = template.deps_type.model_validate(inputs.deps)
        if template.output_for is None:
            result = await agent.run(inputs.prompt, deps=deps, model=model)
        else:
            result = await agent.run(
                inputs.prompt,
                deps=deps,
                model=model,
                output_type=output_types(template.output_for(deps)),
            )
        return result.output

    return task


def dataset_type(
    template: PromptTemplate[BaseModel, TemplateOutput],
    *,
    expected_output_types: tuple[type[BaseModel], ...] | None = None,
) -> type[Dataset[EvalCaseInputs, TemplateOutput, object]]:
    """The `Dataset` class that reads `template`'s cases.

    Subscripted with `template.output_type` (the template's real, runtime output type), not
    the `TemplateOutput` this module is statically typed against: Pydantic Evals deserializes
    `Case.expected_output` and validates it against this generic parameter, and built-ins
    like `EqualsExpected` compare real model instances — subscripting with bare `BaseModel`
    would build an uninstantiable `expected_output` and crash every evaluator that touches
    it, silently, since a crashed evaluator drops out of `report.averages()` rather than
    raising (caught by `raise_on_failures`).
    mypy can't express a generic parameter only known at runtime (there's no static type
    for "whatever this template's output_type happens to be") — `output_type` is a real
    class object at runtime, which is all `Dataset.__class_getitem__` needs.
    A template whose model picks between several outputs (ticket #164) reads its expected
    outputs as their union."""
    readable = expected_output_types or template.output_type
    output_type = reduce(operator.or_, readable) if isinstance(readable, tuple) else readable
    # `output_type` is built at runtime from the template's declared outputs, so mypy cannot
    # accept it as a type argument (valid-type).
    return Dataset[EvalCaseInputs, output_type, object]  # type: ignore[valid-type]


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
    repeat: int = 1,
    promote: bool = False,
    expected_output_types: tuple[type[BaseModel], ...] | None = None,
) -> EvalRunResult:
    """Runs `template_id`'s dataset against `model`, writes a report, reports any regression
    against the committed baseline (ADR-0009), and writes the baseline only when `promote`d.

    `model` and `judge_model` are already-resolved `Model` instances so the caller (the CLI)
    decides whether they're real `OpenRouterModel`s or, in a test, a scripted `FunctionModel`
    — this function never reaches a real model itself. `custom_evaluator_types` lets a
    template's dataset reference custom `Evaluator` subclasses beyond Pydantic Evals' built-ins
    (e.g. `LLMJudge`), since the dataset YAML only stores evaluator names. They grade
    structured output only; a text-output template is graded by built-ins such as `LLMJudge`.
    `repeat` runs every case that many times: one model reply per case is noisy, so the
    baseline averages over all of them. `expected_output_types` reads the dataset with every
    output the template's plugin can return, so a version older than a case's expected output
    still loads it and fails that case (#208); by default, the template's own output types.
    """
    if judge_model_id == model_id:
        raise JudgeModelMatchesModelUnderTestError(model_id)

    set_default_judge_model(judge_model)

    task = eval_task(registry, template_id, version, model=model)
    dataset = dataset_type(
        registry.get(template_id, version), expected_output_types=expected_output_types
    ).from_file(
        datasets_root / f"{template_id}.yaml",
        # The evaluators grade structured outputs (see the docstring above); the dataset's
        # output parameter is only known at runtime, so mypy sees the wider `TemplateOutput`.
        custom_evaluator_types=custom_evaluator_types,  # type: ignore[arg-type]
    )

    report = await dataset.evaluate(
        task, name=f"{template_id} v{version} ({model_id})", repeat=repeat
    )
    report.print(include_input=True, include_output=True)

    reports_root.mkdir(parents=True, exist_ok=True)
    summary = summarize_report(report)
    if rows := jev_check_rows(report):
        print_jev_checks(rows)
        summary = summary.model_copy(
            update={
                "jev_checks": rows,
                "jev_agreement": agreement(rows),
                "jev_agreement_by_rubric": agreement_by_rubric(rows),
                "jev_errored": errored(rows),
            }
        )
    # The Jev fields are left out of a run without checks; the rest keeps its shape.
    (reports_root / f"{template_id}.json").write_text(
        summary.model_dump_json(indent=2, exclude=set() if rows else _JEV_FIELDS) + "\n"
    )

    raise_on_failures(report)
    current = current_baseline(report, template_id=template_id, version=version, model_id=model_id)
    previous, regressions = record_baseline(baselines_root, current, promote=promote)

    return EvalRunResult(
        report=report, baseline=current, previous_baseline=previous, regressions=regressions
    )
