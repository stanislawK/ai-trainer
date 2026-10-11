"""`JevCheck` (ADR-0009 amendment, ADR-0020): one yes/no rubric question about a reply, asked of
Jev next to the `LLMJudge` that stays the judge of record.

Its result is a label (`pass`, `fail` or `errored`), so it never enters the baseline's
assertions or scores until the owner promotes it at a `/tune-prompt` gate. The question lives
in a decision set file; the state is the case's message and the reply as JSON, all dataset
text (ADR-0020 invariant 9).
"""

import json
from dataclasses import dataclass

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext
from pydantic_evals.reporting import EvaluationReport
from rich.console import Console
from rich.table import Table

from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.inputs import EvalCaseInputs

LABEL_PREFIX = "jev:"
# Wide enough that a case name and a reason fit on one line in a log or a terminal.
_TABLE_WIDTH = 200


@dataclass(frozen=True, slots=True)
class _Configured:
    model: Model
    loader: DecisionSetLoader


_configured: _Configured | None = None


def configure_jev_check(model: Model, *, loader: DecisionSetLoader | None = None) -> None:
    """Sets the Jev model every `JevCheck` in the run asks, the way `set_default_judge_model`
    sets `LLMJudge`'s; a test passes a fake `DecisionModel` here."""
    global _configured
    _configured = _Configured(model=model, loader=loader or DecisionSetLoader())


@dataclass
class JevCheck(Evaluator[EvalCaseInputs, object, object]):
    """Asks `rubric`, a probability question of `decision_set`, and passes at or above
    `threshold`. `pairs_with` names the `LLMJudge` assertion it is compared with, by default
    one named like the rubric."""

    decision_set: str
    rubric: str
    threshold: float
    pairs_with: str | None = None
    version: int = 1

    def require_configured(self) -> _Configured:
        if _configured is None:
            raise RuntimeError("JevCheck has no Jev model: call configure_jev_check() first")
        return _configured

    async def evaluate(
        self, ctx: EvaluatorContext[EvalCaseInputs, object, object]
    ) -> dict[str, EvaluationReason]:
        name = LABEL_PREFIX + self.rubric
        try:
            p_yes = await self._ask(ctx)
        except Exception as exc:  # a check never gates, so it never raises into the run
            return {name: EvaluationReason("errored", reason=f"{type(exc).__name__}: {exc}")}
        return {
            name: EvaluationReason(
                "pass" if p_yes >= self.threshold else "fail",
                reason=f"P(yes)={p_yes:.2f} at threshold {self.threshold:g}",
            )
        }

    async def _ask(self, ctx: EvaluatorContext[EvalCaseInputs, object, object]) -> float:
        """A misconfigured check (an unknown set or rubric) errors like a failed call; the
        dataset's own test catches those before a run."""
        configured = self.require_configured()
        output_type = configured.loader.load(
            self.decision_set, self.version, questions={self.rubric}
        ).output_type
        agent: Agent[None, BaseModel] = Agent(configured.model, output_type=output_type)
        state = json.dumps({"athlete_message": ctx.inputs.prompt, "coach_reply": str(ctx.output)})
        result = await agent.run(state)
        return float(getattr(result.output, self.rubric))


class JevCheckRow(BaseModel):
    """One check on one case, next to its paired judge's verdict (`None`: no such judge)."""

    case: str
    rubric: str
    jev: str
    reason: str | None
    judge_name: str
    judge: bool | None
    agree: bool | None


def jev_check_rows[OutputT](
    report: EvaluationReport[EvalCaseInputs, OutputT, object],
) -> list[JevCheckRow]:
    rows: list[JevCheckRow] = []
    for case in report.cases:
        for label in case.labels.values():
            if label.source.name != JevCheck.get_serialization_name():
                continue
            kwargs = label.source.kwargs
            judge_name = str(kwargs.get("pairs_with") or kwargs["rubric"])
            judged = case.assertions.get(judge_name)
            judge = bool(judged.value) if judged is not None else None
            rows.append(
                JevCheckRow(
                    case=case.name,
                    rubric=str(kwargs["rubric"]),
                    jev=label.value,
                    reason=label.reason,
                    judge_name=judge_name,
                    judge=judge,
                    agree=(label.value == "pass") == judge
                    if judge is not None and label.value != "errored"
                    else None,
                )
            )
    return rows


def agreement(rows: list[JevCheckRow]) -> float | None:
    compared = [row.agree for row in rows if row.agree is not None]
    return sum(compared) / len(compared) if compared else None


def agreement_by_rubric(rows: list[JevCheckRow]) -> dict[str, float | None]:
    """Per rubric: a check paired with a judge that asks a broader question (`pain_first` with
    `pain_safety`) shouldn't blur one paired like for like."""
    return {rubric: agreement(_of(rows, rubric)) for rubric in _rubrics(rows)}


def errored(rows: list[JevCheckRow]) -> int:
    return sum(row.jev == "errored" for row in rows)


def print_jev_checks(rows: list[JevCheckRow]) -> None:
    table = Table(title="JevCheck vs LLMJudge")
    for column in ("Case", "Rubric", "Jev", "Reason", "Judge", "Judge verdict", "Agree"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row.case,
            row.rubric,
            row.jev,
            row.reason or "",
            row.judge_name,
            "no judge" if row.judge is None else ("pass" if row.judge else "fail"),
            {True: "agree", False: "disagree", None: "-"}[row.agree],
        )
    Console(width=_TABLE_WIDTH).print(table)
    for rubric in _rubrics(rows):
        picked = _of(rows, rubric)
        print(f"agreement {rubric} vs {picked[0].judge_name}: {_tally(picked)}")
    print(f"agreement: {_tally(rows)}")


def _rubrics(rows: list[JevCheckRow]) -> list[str]:
    return list(dict.fromkeys(row.rubric for row in rows))


def _of(rows: list[JevCheckRow], rubric: str) -> list[JevCheckRow]:
    return [row for row in rows if row.rubric == rubric]


def _tally(rows: list[JevCheckRow]) -> str:
    compared = [row.agree for row in rows if row.agree is not None]
    suffix = f" ({errored(rows)} errored)" if errored(rows) else ""
    if not compared:
        return f"none{suffix}"
    return f"{sum(compared)}/{len(compared)} ({sum(compared) / len(compared):.0%}){suffix}"
