"""Smoke sets (ADR-0034): `evals/smoke/<id>.yaml`, a handful of cases run once each and graded
in code. No repeats, no baseline, no report file.

A set is an ordinary Pydantic Evals dataset plus two optional top-level keys that are read
here and removed before the rest goes to Pydantic Evals:

- `template`: the prompt template the cases run on; defaults to the set's own ID.
- `known_gaps`: case name -> the ticket (`#373`) that will fix it. A failing case listed there
  does not fail the run.

Only the `safety` set holds an `LLMJudge`; every other set is graded without a model call.
"""

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError
from pydantic_ai.models import Model
from pydantic_evals.evaluators import Evaluator

from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.evals.runner import dataset_type, eval_task
from ai_trainer.llm.prompts.registry import PromptRegistry

SAFETY_SMOKE_ID = "safety"
_JUDGE = "LLMJudge"
_TICKET = re.compile(r"#\d+")


class SmokeError(ValueError):
    """A smoke set that can't be run; raised before any model is built."""


class SmokeCase(BaseModel):
    """The parts of a case that the CLI and its tests read; the dataset reads the rest."""

    name: str
    inputs: EvalCaseInputs
    metadata: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SmokeSet:
    id: str
    template_id: str
    known_gaps: Mapping[str, str]
    cases: Sequence[SmokeCase]
    has_judge: bool
    dataset: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SmokeCaseResult:
    name: str
    passed: bool
    detail: str
    gap: str | None

    @property
    def counts_as_failure(self) -> bool:
        return not self.passed and self.gap is None


def smoke_ids(smoke_root: Path) -> list[str]:
    """Every set in `smoke_root`, sorted by ID."""
    return sorted(path.stem for path in smoke_root.glob("*.yaml"))


def _names_judge(evaluators: object) -> bool:
    """Whether a dataset's `evaluators` list (a bare name or a one-key mapping each) has one."""
    if not isinstance(evaluators, list):
        return False
    return any(
        entry == _JUDGE or (isinstance(entry, dict) and _JUDGE in entry) for entry in evaluators
    )


def read_smoke_set(
    smoke_id: str, *, smoke_root: Path, known_templates: Collection[str] | None = None
) -> SmokeSet:
    """The set `smoke_id`, checked end to end so a mistake shows before a model is called."""
    path = smoke_root / f"{smoke_id}.yaml"
    if not path.is_file():
        available = ", ".join(smoke_ids(smoke_root)) or "none"
        raise SmokeError(f"No smoke set named {smoke_id!r}; the sets are: {available}")
    loaded = yaml.safe_load(path.read_text())
    if not isinstance(loaded, dict):
        raise SmokeError(f"{path.name}: expected a mapping at the top level")
    template_id = str(loaded.pop("template", smoke_id))
    raw_gaps = loaded.pop("known_gaps", None) or {}
    known_gaps = {str(name): str(ticket) for name, ticket in raw_gaps.items()}
    try:
        cases = [SmokeCase.model_validate(case) for case in loaded.get("cases", [])]
    except ValidationError as exc:
        raise SmokeError(f"{path.name}: {exc}") from exc
    has_judge = _names_judge(loaded.get("evaluators")) or any(
        _names_judge(case.get("evaluators")) for case in loaded.get("cases", [])
    )

    if known_templates is not None and template_id not in known_templates:
        raise SmokeError(f"{smoke_id}: no prompt template named {template_id!r}")
    case_names = {case.name for case in cases}
    for name, ticket in known_gaps.items():
        if not _TICKET.fullmatch(ticket):
            raise SmokeError(f"{smoke_id}: known gap {name!r} needs a ticket number like #373")
        if name not in case_names:
            raise SmokeError(f"{smoke_id}: known gap {name!r} names no case in the set")
    if has_judge and smoke_id != SAFETY_SMOKE_ID:
        raise SmokeError(f"{smoke_id}: only the safety set may use {_JUDGE}; grade in code")
    if smoke_id == SAFETY_SMOKE_ID and not has_judge:
        raise SmokeError(f"{smoke_id}: the safety set always runs with its judge ({_JUDGE})")
    return SmokeSet(
        id=smoke_id,
        template_id=template_id,
        known_gaps=known_gaps,
        cases=cases,
        has_judge=has_judge,
        dataset=loaded,
    )


async def run_smoke_set(
    smoke_set: SmokeSet,
    *,
    registry: PromptRegistry,
    version: int,
    model: Model,
    model_id: str,
    evaluator_types: Sequence[type[Evaluator[EvalCaseInputs, BaseModel, object]]] = (),
    expected_output_types: tuple[type[BaseModel], ...] | None = None,
) -> list[SmokeCaseResult]:
    """Runs each case once. A case passes when every assertion holds and no evaluator or task
    crashed; a case no evaluator asserted on does not pass."""
    template = registry.get(smoke_set.template_id, version)
    dataset = dataset_type(template, expected_output_types=expected_output_types).from_dict(
        dict(smoke_set.dataset),
        # Same as `run_eval`: structured-output evaluators, the dataset's output type is runtime.
        custom_evaluator_types=evaluator_types,  # type: ignore[arg-type]
    )
    task = eval_task(registry, smoke_set.template_id, version, model=model)
    report = await dataset.evaluate(
        task, name=f"{smoke_set.id} v{version} ({model_id})", progress=False
    )

    results: dict[str, SmokeCaseResult] = {}
    for failure in report.failures:
        results[failure.name] = _result(smoke_set, failure.name, False, failure.error_message)
    for case in report.cases:
        failed = [name for name, result in case.assertions.items() if not result.value]
        errors = [f"{f.name}: {f.error_message}" for f in case.evaluator_failures]
        if errors:
            detail = "evaluator crashed: " + "; ".join(errors)
        elif failed:
            detail = f"failed {', '.join(failed)}; got {case.output!r}"
        elif not case.assertions:
            detail = "no evaluator asserted on this case"
        else:
            detail = ""
        results[case.name] = _result(smoke_set, case.name, not detail, detail)
    return [results[case.name] for case in smoke_set.cases if case.name in results]


def _result(smoke_set: SmokeSet, name: str, passed: bool, detail: str) -> SmokeCaseResult:
    return SmokeCaseResult(
        name=name, passed=passed, detail=detail, gap=smoke_set.known_gaps.get(name)
    )
