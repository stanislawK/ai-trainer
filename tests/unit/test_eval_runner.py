from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals.evaluators import EqualsExpected, Evaluator, EvaluatorContext

from ai_trainer.llm.evals.baseline import Baseline, BaselineThresholds, save_baseline
from ai_trainer.llm.evals.runner import (
    CaseFailedError,
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
    promote: bool = False,
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
        promote=promote,
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


async def test_a_promoted_first_run_with_no_baseline_writes_one(tmp_path: Path) -> None:
    result = await _run(tmp_path, promote=True)

    assert result.previous_baseline is None
    assert result.regressions == []

    baseline_path = tmp_path / "baselines" / "sample.json"
    assert baseline_path.exists()
    assert result.baseline.template_id == "sample"
    assert result.baseline.model == _MODEL_UNDER_TEST_ID
    assert result.baseline.assertions == 1.0
    assert result.baseline.scores["_MentionsNameScore"] == 1.0


async def test_second_run_diffs_against_the_existing_baseline(tmp_path: Path) -> None:
    await _run(tmp_path, promote=True)

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


def _write_two_case_dataset(path: Path) -> None:
    from pydantic_evals import Case, Dataset

    cases = [
        Case(
            name=name,
            inputs=EvalCaseInputs(prompt=prompt, deps={"name": "Alex", "sport": "climbing"}),
            expected_output=_Output(reply="hi Alex"),
        )
        for name, prompt in (("greet_alex", "hi"), ("unreachable", "fail"))
    ]
    dataset = Dataset[EvalCaseInputs, _Output, object](
        name="sample", cases=cases, evaluators=[EqualsExpected()]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_file(path)


def _prompt_of(messages: list[ModelMessage]) -> str:
    request = messages[-1]
    assert isinstance(request, ModelRequest)
    return next(str(part.content) for part in request.parts if isinstance(part, UserPromptPart))


def _unauthorized(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise ModelHTTPError(
        status_code=401, model_name=_MODEL_UNDER_TEST_ID, body={"message": "No auth credentials"}
    )


def _fails_on_fail_prompt(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if _prompt_of(messages) == "fail":
        return _unauthorized(messages, info)
    return _reply_response(messages, info)


async def _run_two_cases(tmp_path: Path, respond: FunctionModel) -> EvalRunResult:
    _write_two_case_dataset(tmp_path / "datasets" / "sample.yaml")
    return await run_eval(
        registry=_registry(),
        template_id="sample",
        version=1,
        model=respond,
        model_id=_MODEL_UNDER_TEST_ID,
        judge_model=FunctionModel(_reply_response),
        judge_model_id=_JUDGE_MODEL_ID,
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )


async def test_cases_that_all_fail_raise_instead_of_writing_a_perfect_baseline(
    tmp_path: Path,
) -> None:
    """A wrong API key fails every case. A failed case drops out of
    `report.averages()`, so without this guard the run wrote `assertions: 1.0` and exited 0
    (found at #109)."""
    with pytest.raises(CaseFailedError) as exc_info:
        await _run_two_cases(tmp_path, FunctionModel(_unauthorized))

    assert "2 of 2 cases failed" in str(exc_info.value)
    assert "401" in str(exc_info.value)
    assert not (tmp_path / "baselines" / "sample.json").exists()
    assert (tmp_path / "reports" / "sample.json").exists()


async def test_one_failed_case_raises_instead_of_writing_an_inflated_baseline(
    tmp_path: Path,
) -> None:
    with pytest.raises(CaseFailedError) as exc_info:
        await _run_two_cases(tmp_path, FunctionModel(_fails_on_fail_prompt))

    assert "1 of 2 cases failed" in str(exc_info.value)
    assert "unreachable" in str(exc_info.value)
    assert exc_info.value.failures == ["unreachable"]
    assert not (tmp_path / "baselines" / "sample.json").exists()


async def test_fails_fast_when_judge_model_matches_model_under_test(tmp_path: Path) -> None:
    with pytest.raises(JudgeModelMatchesModelUnderTestError) as exc_info:
        await _run(tmp_path, model_id="same/model", judge_model_id="same/model")

    assert "same/model" in str(exc_info.value)


_TEXT_TEMPLATE = PromptTemplate(
    id="sample",
    version=1,
    locale="en",
    deps_type=_Deps,
    output_type=str,
    model_settings_key="sample_template_model",
)


def _judge_passes(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[
            ToolCallPart(
                info.output_tools[0].name, {"reason": "warm and safe", "pass": True, "score": 1.0}
            )
        ]
    )


async def test_a_text_output_template_is_graded_by_a_named_llm_judge(tmp_path: Path) -> None:
    """A reply template returns plain text (ADR-0008 amendment, #81); its dataset grades it
    with `LLMJudge` through the judge model set by `run_eval`, never a real model."""
    from pydantic_evals import Case, Dataset
    from pydantic_evals.evaluators import LLMJudge

    dataset = Dataset[EvalCaseInputs, str, object](
        name="sample",
        cases=[
            Case(
                name="greet_alex",
                inputs=EvalCaseInputs(prompt="hi", deps={"name": "Alex", "sport": "climbing"}),
            )
        ],
        evaluators=[
            LLMJudge(rubric="Warm", include_input=True, assertion={"evaluation_name": "tone"})
        ],
    )
    (tmp_path / "datasets").mkdir()
    dataset.to_file(tmp_path / "datasets" / "sample.yaml")
    registry = PromptRegistry(root=_FIXTURES_ROOT)
    registry.register(_TEXT_TEMPLATE)

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Hi Alex!")])

    result = await run_eval(
        registry=registry,
        template_id="sample",
        version=1,
        model=FunctionModel(reply),
        model_id=_MODEL_UNDER_TEST_ID,
        judge_model=FunctionModel(_judge_passes),
        judge_model_id=_JUDGE_MODEL_ID,
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        promote=True,
    )

    case = result.report.cases[0]
    assert case.output == "Hi Alex!"
    assert case.assertions["tone"].value is True
    assert result.baseline.assertions == 1.0
    assert (tmp_path / "baselines" / "sample.json").exists()


async def test_repeat_runs_each_case_n_times_and_averages_them_into_the_baseline(
    tmp_path: Path,
) -> None:
    """Single replies are noisy; `repeat` samples each case several times (#81)."""
    _write_sample_dataset(tmp_path / "datasets" / "sample.yaml")
    replies = iter(['{"reply": "hi Alex"}', '{"reply": "hello"}'])

    def alternating(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(next(replies))])

    result = await run_eval(
        registry=_registry(),
        template_id="sample",
        version=1,
        model=FunctionModel(alternating),
        model_id=_MODEL_UNDER_TEST_ID,
        judge_model=FunctionModel(_reply_response),
        judge_model_id=_JUDGE_MODEL_ID,
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        custom_evaluator_types=[_MentionsNameScore],
        repeat=2,
    )

    assert len(result.report.cases) == 2
    assert result.baseline.scores["_MentionsNameScore"] == 0.5


class _Question(BaseModel):
    ask: str


_TWO_OUTPUT_TEMPLATE = PromptTemplate(
    id="sample",
    version=1,
    locale="en",
    deps_type=_Deps,
    output_type=(_Output, _Question),
    model_settings_key="sample_template_model",
)


async def test_a_template_with_two_outputs_loads_and_grades_either_expected_output(
    tmp_path: Path,
) -> None:
    """An extraction may return a question instead of a draft (ticket #164): its dataset holds
    expected outputs of either type, and the model's pick is graded against them."""
    from pydantic_evals import Case, Dataset

    dataset = Dataset[EvalCaseInputs, _Output | _Question, object](
        name="sample",
        cases=[
            Case(
                name="asks",
                inputs=EvalCaseInputs(prompt="?", deps={"name": "Alex", "sport": "climbing"}),
                expected_output=_Question(ask="which?"),
            ),
        ],
        evaluators=[EqualsExpected()],
    )
    (tmp_path / "datasets").mkdir()
    dataset.to_file(tmp_path / "datasets" / "sample.yaml")
    registry = PromptRegistry(root=_FIXTURES_ROOT)
    registry.register(_TWO_OUTPUT_TEMPLATE)

    def ask(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        [tool] = [tool.name for tool in info.output_tools if "_Question" in tool.name]
        return ModelResponse(parts=[ToolCallPart(tool, {"ask": "which?"})])

    result = await run_eval(
        registry=registry,
        template_id="sample",
        version=1,
        model=FunctionModel(ask),
        model_id=_MODEL_UNDER_TEST_ID,
        judge_model=FunctionModel(ask),
        judge_model_id=_JUDGE_MODEL_ID,
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )

    assert result.baseline.assertions == 1.0


async def test_a_case_runs_with_the_output_its_deps_narrow_the_template_to(
    tmp_path: Path,
) -> None:
    """An answered extraction can only draft in the app, and so in its eval (ticket #164)."""
    from pydantic_evals import Case, Dataset

    def draft_only_for_alex(
        deps: BaseModel,
    ) -> type[_Output | _Question] | tuple[type[_Output | _Question], ...]:
        return _Output if isinstance(deps, _Deps) and deps.name == "Alex" else (_Output, _Question)

    template = PromptTemplate(
        id="sample",
        version=1,
        locale="en",
        deps_type=_Deps,
        output_type=(_Output, _Question),
        model_settings_key="sample_template_model",
        output_for=draft_only_for_alex,
    )
    (tmp_path / "datasets").mkdir()
    Dataset[EvalCaseInputs, _Output | _Question, object](
        name="sample",
        cases=[
            Case(
                name="narrowed",
                inputs=EvalCaseInputs(prompt="?", deps={"name": "Alex", "sport": "climbing"}),
                expected_output=_Output(reply="hi"),
            )
        ],
        evaluators=[EqualsExpected()],
    ).to_file(tmp_path / "datasets" / "sample.yaml")
    registry = PromptRegistry(root=_FIXTURES_ROOT)
    registry.register(template)
    offered: list[list[str]] = []

    def reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.append([tool.name for tool in info.output_tools])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"reply": "hi"})])

    result = await run_eval(
        registry=registry,
        template_id="sample",
        version=1,
        model=FunctionModel(reply),
        model_id=_MODEL_UNDER_TEST_ID,
        judge_model=FunctionModel(reply),
        judge_model_id=_JUDGE_MODEL_ID,
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
    )

    assert offered == [["final_result"]]
    assert result.baseline.assertions == 1.0


# A run diffs against the committed baseline and writes it only when promoted (#208).


def _regressed_baseline(tmp_path: Path) -> Path:
    path = tmp_path / "baselines" / "sample.json"
    save_baseline(
        path,
        Baseline(
            template_id="sample",
            version=1,
            model=_MODEL_UNDER_TEST_ID,
            assertions=1.0,
            scores={"_MentionsNameScore": 1.0},
            thresholds=BaselineThresholds(max_assertions_drop=0.1, max_score_drop=0.2),
        ),
    )
    return path


def _no_name_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart('{"reply": "hello there"}')])


async def _run_regressing(tmp_path: Path, *, promote: bool) -> EvalRunResult:
    _write_sample_dataset(tmp_path / "datasets" / "sample.yaml")
    return await run_eval(
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
        promote=promote,
    )


async def test_a_run_without_promote_writes_no_baseline(tmp_path: Path) -> None:
    result = await _run(tmp_path)

    assert result.baseline.assertions == 1.0
    assert (tmp_path / "reports" / "sample.json").exists()
    assert not (tmp_path / "baselines" / "sample.json").exists()


async def test_a_regressing_run_without_promote_leaves_the_baseline_byte_identical(
    tmp_path: Path,
) -> None:
    path = _regressed_baseline(tmp_path)
    before = path.read_bytes()

    result = await _run_regressing(tmp_path, promote=False)

    assert result.regressions
    assert path.read_bytes() == before


async def test_a_promoted_regression_writes_the_new_figures_with_the_committed_thresholds(
    tmp_path: Path,
) -> None:
    path = _regressed_baseline(tmp_path)

    result = await _run_regressing(tmp_path, promote=True)

    assert result.regressions
    saved = Baseline.model_validate_json(path.read_text())
    assert saved.assertions == 0.0
    assert saved.scores == {"_MentionsNameScore": 0.0}
    assert saved.thresholds == BaselineThresholds(max_assertions_drop=0.1, max_score_drop=0.2)


async def test_an_older_version_reads_the_cases_it_cannot_return_as_failed_cases(
    tmp_path: Path,
) -> None:
    """A version from before a template could ask still loads a dataset holding question cases
    when the run names every output the template's plugin can return; the case's draft then
    fails the comparison instead of the load (#208)."""
    from pydantic_evals import Case, Dataset

    (tmp_path / "datasets").mkdir()
    Dataset[EvalCaseInputs, _Output | _Question, object](
        name="sample",
        cases=[
            Case(
                name="asks",
                inputs=EvalCaseInputs(prompt="?", deps={"name": "Alex", "sport": "climbing"}),
                expected_output=_Question(ask="which?"),
            ),
            Case(
                name="drafts",
                inputs=EvalCaseInputs(prompt="hi", deps={"name": "Alex", "sport": "climbing"}),
                expected_output=_Output(reply="hi Alex"),
            ),
        ],
        evaluators=[EqualsExpected()],
    ).to_file(tmp_path / "datasets" / "sample.yaml")

    result = await run_eval(
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
        expected_output_types=(_Output, _Question),
    )

    grades = {case.name: case.assertions["EqualsExpected"].value for case in result.report.cases}
    assert grades == {"asks": False, "drafts": True}
