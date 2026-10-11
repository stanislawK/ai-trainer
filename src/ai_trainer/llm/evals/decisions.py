"""Decision-set evals (ADR-0009 amendment, ADR-0020): a set's dataset run through a
`DecisionModel`, its labels graded in code, and each case's probabilities recorded.

A case's `expected_output` maps question names to labels: `true`/`false` for a probability
question, graded at its threshold from `Settings` (never the library's 0.5 `bool` cut), and
an option name or level for a choice or score, which must match exactly. A question with no
label is answered and recorded, but not graded.
"""

from dataclasses import dataclass
from pathlib import Path
from statistics import fmean

from pydantic import BaseModel, TypeAdapter
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_evals import Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext
from pydantic_evals.reporting import EvaluationReport

from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.baseline import Baseline, CaseProbability, find_changes
from ai_trainer.llm.evals.runner import (
    EvalCaseInputs,
    EvalReportSummary,
    current_baseline,
    raise_on_failures,
    record_baseline,
    summarize_report,
)

_LABELS = TypeAdapter(dict[str, bool | int | str])

type CaseProbabilities = dict[str, dict[str, CaseProbability]]


class DecisionEvalOutput(BaseModel):
    """One answered state: each question's answer, and the number behind it that jitters
    between calls — P(yes) for a probability, the picked option's probability for a choice,
    and the expected level for a score. Jev rounds that expected level into the answer, so the
    rounded level's own probability can be 0 and would hide the jitter."""

    answers: dict[str, int | float | str]
    probabilities: dict[str, float]


@dataclass
class DecisionLabelsMatch(Evaluator[EvalCaseInputs, object, object]):
    """One assertion per labelled question, named after it."""

    thresholds: dict[str, float]

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, object, object]) -> dict[str, bool]:
        assert isinstance(ctx.output, DecisionEvalOutput)
        results: dict[str, bool] = {}
        for name, label in _LABELS.validate_python(ctx.expected_output or {}).items():
            if name not in ctx.output.answers:
                raise ValueError(f"Label {name!r} names no question in the set")
            answer = ctx.output.answers[name]
            if isinstance(label, bool):
                if name not in self.thresholds:
                    raise ValueError(f"No threshold in Settings for probability {name!r}")
                results[name] = (float(answer) >= self.thresholds[name]) == label
            else:
                results[name] = answer == label
        return results


class DecisionReportSummary(EvalReportSummary):
    """`evals/reports/<decision_id>.json` (gitignored): the usual summary plus probabilities."""

    probabilities: CaseProbabilities


@dataclass(frozen=True, slots=True)
class DecisionEvalRunResult:
    report: EvaluationReport[EvalCaseInputs, object, object]
    baseline: Baseline
    previous_baseline: Baseline | None
    regressions: list[str]
    changes: list[str]


async def run_decision_eval(
    *,
    loader: DecisionSetLoader,
    decision_id: str,
    version: int,
    model: Model,
    model_id: str,
    thresholds: dict[str, float],
    datasets_root: Path,
    baselines_root: Path,
    reports_root: Path,
    repeat: int = 1,
) -> DecisionEvalRunResult:
    """Runs `decision_id`'s dataset against `model`, writes a report and a baseline holding
    `thresholds` and each case's probabilities, and diffs it against the committed one.

    No `llm_calls` row: no user is acting, and the dataset holds no athlete text."""
    decision_set = loader.load(decision_id, version)
    agent: Agent[None, BaseModel] = Agent(model, output_type=decision_set.output_type)

    async def task(inputs: EvalCaseInputs) -> DecisionEvalOutput:
        result = await agent.run(inputs.prompt)
        details = result.response.provider_details or {}
        distributions: dict[str, dict[str, float]] = details.get("probabilities", {})
        expected_levels: dict[str, float] = details.get("scores", {})
        answers = result.output.model_dump(mode="json")
        return DecisionEvalOutput(
            answers=answers,
            probabilities={
                name: value_behind(answer, distributions.get(name), expected_levels.get(name))
                for name, answer in answers.items()
            },
        )

    dataset = Dataset[EvalCaseInputs, object, object].from_file(
        datasets_root / f"{decision_id}.yaml"
    )
    dataset.add_evaluator(DecisionLabelsMatch(thresholds=thresholds))
    print(
        "graded at thresholds: "
        + (", ".join(f"{name} >= {value:g}" for name, value in thresholds.items()) or "none")
    )
    report = await dataset.evaluate(
        task, name=f"{decision_id} v{version} ({model_id})", repeat=repeat
    )
    report.print(include_input=True, include_output=True)

    probabilities = case_probabilities(report)
    print_spread(probabilities)
    reports_root.mkdir(parents=True, exist_ok=True)
    summary = DecisionReportSummary(
        **summarize_report(report).model_dump(), probabilities=probabilities
    )
    (reports_root / f"{decision_id}.json").write_text(summary.model_dump_json(indent=2) + "\n")

    raise_on_failures(report)
    current = current_baseline(
        report, template_id=decision_id, version=version, model_id=model_id
    ).model_copy(update={"decision_thresholds": thresholds, "cases": probabilities})
    previous, regressions = record_baseline(baselines_root, current)
    return DecisionEvalRunResult(
        report=report,
        baseline=current,
        previous_baseline=previous,
        regressions=regressions,
        changes=find_changes(previous=previous, current=current) if previous is not None else [],
    )


def value_behind(
    answer: int | float | str, distribution: dict[str, float] | None, expected_level: float | None
) -> float:
    if expected_level is not None:
        return float(expected_level)
    if distribution is not None:
        return distribution[str(answer)]
    return float(answer)


def case_probabilities(
    report: EvaluationReport[EvalCaseInputs, object, object],
) -> CaseProbabilities:
    """Each question's value per case, gathered over that case's repeats that ran, and whether
    its grade, or a choice's or score's answer, came out differently between them."""
    values: dict[str, dict[str, list[float]]] = {}
    grades: dict[str, dict[str, set[bool | str]]] = {}
    for case in report.cases:
        assert isinstance(case.output, DecisionEvalOutput)
        key = case.source_case_name or case.name
        for name, value in case.output.probabilities.items():
            values.setdefault(key, {}).setdefault(name, []).append(value)
        for name, answer in case.output.answers.items():
            # A probability's answer is the jittering float itself; its flips show in its grade.
            if not isinstance(answer, float):
                grades.setdefault(key, {}).setdefault(f"answer:{name}", set()).add(str(answer))
        for name, assertion in case.assertions.items():
            grades.setdefault(key, {}).setdefault(name, set()).add(bool(assertion.value))
    return {
        case: {
            name: CaseProbability(
                mean=fmean(runs),
                min=min(runs),
                max=max(runs),
                runs=len(runs),
                flips=any(
                    len(grades.get(case, {}).get(seen, set())) > 1
                    for seen in (name, f"answer:{name}")
                ),
            )
            for name, runs in questions.items()
        }
        for case, questions in values.items()
    }


def print_spread(probabilities: CaseProbabilities) -> None:
    print(
        "probability spread per case (P(yes), a choice's P(pick) or a score's expected level; "
        "FLIPS when the grade differed between repeats):"
    )
    for case, questions in probabilities.items():
        for name, stats in questions.items():
            print(
                f"  {case} / {name}: mean {stats.mean:.2f}, min {stats.min:.2f}, "
                f"max {stats.max:.2f}, spread {stats.spread:.2f} over {stats.runs} runs"
                + ("  FLIPS" if stats.flips else "")
            )
