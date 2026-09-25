from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic_ai import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals.evaluators import EqualsExpected, Evaluator, EvaluatorContext

from ai_trainer.llm.evals.baseline import Baseline, save_baseline
from ai_trainer.llm.evals.runner import (
    EvalCaseInputs,
    EvalRunResult,
    EvaluatorFailedError,
    JudgeModelMatchesModelUnderTestError,
    run_eval,
)
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplate

_FIXTURES_ROOT = Path(__file__).parent.parent / "fixtures" / "prompts"

_MODEL_UNDER_TEST_ID = "test/under-test"
_JUDGE_MODEL_ID = "test/judge"


class _Deps(BaseModel):
    name: str
    sport: str


class _Output(BaseModel):
    reply: str


_SAMPLE_TEMPLATE = PromptTemplate(
    id="sample",
    version=1,
    locale="en",
    deps_type=_Deps,
    output_type=_Output,
    model_settings_key="sample_template_model",
)


@dataclass
class _MentionsNameScore(Evaluator[EvalCaseInputs, BaseModel]):
    """A code-graded evaluator contributing a score, not just an assertion, so the sample
    dataset exercises both halves of ADR-0009's baseline (assertions and scores).

    Typed against `BaseModel`, not `_Output`, to match `run_eval`'s own generic output type
    (it doesn't know a template's concrete output type statically) — Pydantic Evals'
    `Evaluator` is invariant in its output type parameter, so a `Evaluator[..., _Output]`
    isn't assignable where `Evaluator[..., BaseModel]` is expected.
    """

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> float:
        assert isinstance(ctx.output, _Output)
        return 1.0 if str(ctx.inputs.deps.get("name", "")) in ctx.output.reply else 0.0


@dataclass
class _CrashingEvaluator(Evaluator[EvalCaseInputs, BaseModel]):
    """Raises unconditionally, to prove a crashed evaluator fails the run loudly instead of
    silently dropping out of the averages (`run_eval`'s `EvaluatorFailedError` guard)."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> float:
        raise RuntimeError("boom")


def _registry() -> PromptRegistry:
    registry = PromptRegistry(root=_FIXTURES_ROOT)
    registry.register(_SAMPLE_TEMPLATE)
    return registry


def _write_sample_dataset(path: Path) -> None:
    from pydantic_evals import Case, Dataset

    case = Case(
        name="greet_alex",
        inputs=EvalCaseInputs(prompt="hi", deps={"name": "Alex", "sport": "climbing"}),
        expected_output=_Output(reply="hi Alex"),
    )
    dataset = Dataset[EvalCaseInputs, _Output, object](
        name="sample",
        cases=[case],
        evaluators=[EqualsExpected(), _MentionsNameScore()],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_file(path)


def _write_crashing_dataset(path: Path) -> None:
    from pydantic_evals import Case, Dataset

    case = Case(
        name="greet_alex",
        inputs=EvalCaseInputs(prompt="hi", deps={"name": "Alex", "sport": "climbing"}),
        expected_output=_Output(reply="hi Alex"),
    )
    dataset = Dataset[EvalCaseInputs, _Output, object](
        name="sample",
        cases=[case],
        evaluators=[_CrashingEvaluator()],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_file(path)


def _reply_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart('{"reply": "hi Alex"}')])


async def _run(
    tmp_path: Path,
    *,
    model_id: str = _MODEL_UNDER_TEST_ID,
    judge_model_id: str = _JUDGE_MODEL_ID,
) -> EvalRunResult:
    _write_sample_dataset(tmp_path / "datasets" / "sample.yaml")
    return await run_eval(
        registry=_registry(),
        template_id="sample",
        version=1,
        model=FunctionModel(_reply_response),
        model_id=model_id,
        judge_model=FunctionModel(_reply_response),
        judge_model_id=judge_model_id,
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        custom_evaluator_types=[_MentionsNameScore],
    )


async def test_run_against_sample_dataset_writes_report_and_prints_per_case_scores(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = await _run(tmp_path)

    output = result.report.cases[0].output
    assert isinstance(output, _Output)
    assert output.reply == "hi Alex"
    assert result.report.cases[0].scores["_MentionsNameScore"].value == 1.0

    report_path = tmp_path / "reports" / "sample.json"
    assert report_path.exists()
    assert "_MentionsNameScore" in report_path.read_text()

    # `report.print()` renders a Rich table sized to the terminal width, which truncates a
    # long evaluator name in a narrow, non-tty capture — assert on the case row and the
    # "Scores" column header instead of the (possibly truncated) evaluator name itself.
    console_output = capsys.readouterr().out
    assert "greet_alex" in console_output
    assert "Scores" in console_output


async def test_first_run_with_no_baseline_writes_one(tmp_path: Path) -> None:
    result = await _run(tmp_path)

    assert result.previous_baseline is None
    assert result.regressions == []

    baseline_path = tmp_path / "baselines" / "sample.json"
    assert baseline_path.exists()
    assert result.baseline.template_id == "sample"
    assert result.baseline.model == _MODEL_UNDER_TEST_ID
    assert result.baseline.assertions == 1.0
    assert result.baseline.scores["_MentionsNameScore"] == 1.0


async def test_second_run_diffs_against_the_existing_baseline(tmp_path: Path) -> None:
    await _run(tmp_path)

    result = await _run(tmp_path)

    assert result.previous_baseline is not None
    assert result.previous_baseline.template_id == "sample"
    assert result.regressions == []


async def test_score_below_baseline_threshold_is_reported_as_a_regression(tmp_path: Path) -> None:
    save_baseline(
        tmp_path / "baselines" / "sample.json",
        Baseline(
            template_id="sample",
            version=1,
            model=_MODEL_UNDER_TEST_ID,
            assertions=1.0,
            scores={"_MentionsNameScore": 1.0},
        ),
    )

    def _no_name_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart('{"reply": "hello there"}')])

    _write_sample_dataset(tmp_path / "datasets" / "sample.yaml")
    result = await run_eval(
        registry=_registry(),
        template_id="sample",
        version=1,
        model=FunctionModel(_no_name_response),
        model_id=_MODEL_UNDER_TEST_ID,
        judge_model=FunctionModel(_reply_response),
        judge_model_id=_JUDGE_MODEL_ID,
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        custom_evaluator_types=[_MentionsNameScore],
    )

    # The wrong reply fails both the exact-match assertion (EqualsExpected) and the custom
    # score evaluator, so both halves of ADR-0009's baseline flag a regression.
    assert len(result.regressions) == 2
    assert any("assertions" in regression for regression in result.regressions)
    assert any("_MentionsNameScore" in regression for regression in result.regressions)


async def test_a_crashing_evaluator_raises_instead_of_writing_a_baseline(
    tmp_path: Path,
) -> None:
    """A run where an evaluator raises must not be treated as a clean pass: `report.averages()`
    silently drops a crashed evaluator's contribution rather than raising itself, so `run_eval`
    checks for evaluator failures explicitly and refuses to compute or write a baseline from
    a run that had any."""
    _write_crashing_dataset(tmp_path / "datasets" / "sample.yaml")

    with pytest.raises(EvaluatorFailedError) as exc_info:
        await run_eval(
            registry=_registry(),
            template_id="sample",
            version=1,
            model=FunctionModel(_reply_response),
            model_id=_MODEL_UNDER_TEST_ID,
            judge_model=FunctionModel(_reply_response),
            judge_model_id=_JUDGE_MODEL_ID,
            datasets_root=tmp_path / "datasets",
            baselines_root=tmp_path / "baselines",
            reports_root=tmp_path / "reports",
            custom_evaluator_types=[_CrashingEvaluator],
        )

    assert "_CrashingEvaluator" in str(exc_info.value)
    assert "boom" in str(exc_info.value)
    assert not (tmp_path / "baselines" / "sample.json").exists()


async def test_fails_fast_when_judge_model_matches_model_under_test(tmp_path: Path) -> None:
    with pytest.raises(JudgeModelMatchesModelUnderTestError) as exc_info:
        await _run(tmp_path, model_id="same/model", judge_model_id="same/model")

    assert "same/model" in str(exc_info.value)
