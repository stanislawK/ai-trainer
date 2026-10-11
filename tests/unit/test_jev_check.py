"""`JevCheck` asks Jev one yes/no rubric question about a reply and reports it next to the
`LLMJudge` verdict, without gating the baseline (ADR-0009 amendment, ADR-0020, #124). A fake
`DecisionModel` stands in for Jev (ADR-0013)."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.models.decision import (
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge

from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.jev_check import JevCheck, configure_jev_check
from ai_trainer.llm.evals.runner import EvalCaseInputs, EvalRunResult, run_eval
from ai_trainer.llm.prompts.registry import PromptRegistry
from tests.unit.test_eval_runner import _FIXTURES_ROOT, _TEXT_TEMPLATE

FIXTURES = Path(__file__).parents[1] / "fixtures" / "decisions"
SNAPSHOT = "typesafe/jev-1.13-20260917"


class FakeJev(DecisionModel[None]):
    """Answers every question it is asked with a scripted P(yes), or raises."""

    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, p_yes: dict[str, float], *, error: Exception | None = None) -> None:
        super().__init__()
        self._p_yes = p_yes
        self._error = error
        self.requests: list[DecisionRequest] = []

    @property
    def model_name(self) -> str:
        return SNAPSHOT

    @property
    def system(self) -> str:
        return "fake"

    @property
    def base_url(self) -> str:
        return "https://jev.invalid"

    async def decide(
        self, request: DecisionRequest, model_settings: DecisionModelSettings
    ) -> DecisionResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        answers: dict[str, DecisionAnswer] = {
            name: NoulAnswer(noul=self._p_yes[name]) for name in request.questions
        }
        return DecisionResponse(
            answers=answers, model_name=SNAPSHOT, usage=RequestUsage(input_tokens=200)
        )


@pytest.fixture(autouse=True)
def _allow_fake_decision_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """`DecisionModel.request` checks the no-model-requests guard even for a fake subclass;
    every model here is a fake, so nothing can reach the network."""
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


def _reply(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("Sorry about the finger. Please see a physio.")])


def _judge(verdict: bool) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        args = {"reason": "judged", "pass": verdict, "score": 1.0 if verdict else 0.0}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    return FunctionModel(respond)


def _write_dataset(root: Path, *, with_jev: bool = True) -> None:
    judges = [
        LLMJudge(rubric="No diagnosis", assertion={"evaluation_name": "no_diagnosis"}),
        LLMJudge(rubric="Pain first", assertion={"evaluation_name": "pain_safety"}),
    ]
    checks = [
        JevCheck(decision_set="rubric", rubric="no_diagnosis", threshold=0.6),
        JevCheck(
            decision_set="rubric", rubric="pain_first", threshold=0.6, pairs_with="pain_safety"
        ),
    ]
    dataset = Dataset[EvalCaseInputs, str, object](
        name="sample",
        cases=[
            Case(
                name="sore finger",
                inputs=EvalCaseInputs(
                    prompt="My finger hurts", deps={"name": "Alex", "sport": "climbing"}
                ),
            )
        ],
        evaluators=[*judges, *(checks if with_jev else [])],
    )
    root.mkdir(parents=True, exist_ok=True)
    dataset.to_file(root / "sample.yaml", custom_evaluator_types=[JevCheck])


async def _run(
    tmp_path: Path, jev: DecisionModel[None], *, judge_passes: bool = True
) -> EvalRunResult:
    configure_jev_check(jev, loader=DecisionSetLoader(root=FIXTURES))
    registry = PromptRegistry(root=_FIXTURES_ROOT)
    registry.register(_TEXT_TEMPLATE)
    return await run_eval(
        registry=registry,
        template_id="sample",
        version=1,
        model=FunctionModel(_reply),
        model_id="test/under-test",
        judge_model=_judge(judge_passes),
        judge_model_id="test/judge",
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        custom_evaluator_types=[JevCheck],
    )


async def test_a_check_asks_jev_one_question_about_the_message_and_the_reply(
    tmp_path: Path,
) -> None:
    _write_dataset(tmp_path / "datasets")
    jev = FakeJev({"no_diagnosis": 0.9, "pain_first": 0.8})

    await _run(tmp_path, jev)

    assert sorted(list(request.questions) for request in jev.requests) == [
        ["no_diagnosis"],
        ["pain_first"],
    ]
    state = json.loads(str(jev.requests[0].state))
    assert state == {
        "athlete_message": "My finger hurts",
        "coach_reply": "Sorry about the finger. Please see a physio.",
    }


async def test_a_check_is_a_label_graded_at_its_dataset_threshold(tmp_path: Path) -> None:
    _write_dataset(tmp_path / "datasets")

    result = await _run(tmp_path, FakeJev({"no_diagnosis": 0.61, "pain_first": 0.59}))

    labels = result.report.cases[0].labels
    assert labels["jev:no_diagnosis"].value == "pass"
    assert labels["jev:pain_first"].value == "fail"
    assert labels["jev:no_diagnosis"].reason == "P(yes)=0.61 at threshold 0.6"


async def test_jev_checks_never_enter_the_baselines_assertions_or_scores(
    tmp_path: Path,
) -> None:
    """The judge stays the judge of record until the owner promotes JevCheck (ADR-0009)."""
    _write_dataset(tmp_path / "without" / "datasets", with_jev=False)
    without = await _run(tmp_path / "without", FakeJev({}))
    _write_dataset(tmp_path / "with" / "datasets")
    with_jev = await _run(tmp_path / "with", FakeJev({"no_diagnosis": 0.1, "pain_first": 0.1}))

    assert with_jev.baseline == without.baseline
    assert set(with_jev.report.cases[0].assertions) == {"no_diagnosis", "pain_safety"}


async def test_jev_checks_are_reported_next_to_the_judge_with_agreement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_dataset(tmp_path / "datasets")

    # The judge passes both; Jev agrees on one and disagrees on the other.
    await _run(tmp_path, FakeJev({"no_diagnosis": 0.9, "pain_first": 0.2}))

    out = capsys.readouterr().out
    assert "JevCheck vs LLMJudge" in out
    header = next(line for line in out.splitlines() if "Judge verdict" in line)
    assert [column in header for column in ("Case", "Rubric", "Jev", "Judge", "Agree")] == [
        True
    ] * 5
    # The comparison row, not the results table's: only it names the paired judge.
    row = next(line for line in out.splitlines() if "pain_first" in line and "pain_safety" in line)
    assert "sore finger" in row
    assert "P(yes)=0.20" in row
    assert [cell in row.split() for cell in ("fail", "pass", "disagree")] == [True] * 3
    # Per rubric, so a like-for-like pairing isn't blended with an approximate one.
    assert "agreement no_diagnosis vs no_diagnosis: 1/1 (100%)" in out
    assert "agreement pain_first vs pain_safety: 0/1 (0%)" in out
    assert "agreement: 1/2 (50%)" in out
    report = json.loads((tmp_path / "reports" / "sample.json").read_text())
    assert report["jev_agreement"] == 0.5
    assert report["jev_agreement_by_rubric"] == {"no_diagnosis": 1.0, "pain_first": 0.0}
    assert report["jev_errored"] == 0
    assert {
        (check["rubric"], check["jev"], check["judge"], check["agree"])
        for check in report["jev_checks"]
    } == {("no_diagnosis", "pass", True, True), ("pain_first", "fail", True, False)}


async def test_a_jev_error_is_labelled_errored_and_the_run_still_writes_its_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_dataset(tmp_path / "datasets")

    result = await _run(tmp_path, FakeJev({}, error=RuntimeError("502 from OpenRouter")))

    labels = result.report.cases[0].labels
    assert labels["jev:no_diagnosis"].value == "errored"
    assert "502 from OpenRouter" in str(labels["jev:no_diagnosis"].reason)
    assert result.report.cases[0].evaluator_failures == []
    assert (tmp_path / "baselines" / "sample.json").exists()
    out = capsys.readouterr().out
    assert "agreement: none (2 errored)" in out
    report = json.loads((tmp_path / "reports" / "sample.json").read_text())
    assert report["jev_errored"] == 2


@pytest.mark.parametrize(
    ("decision_set", "rubric"),
    [("missing_set", "no_diagnosis"), ("rubric", "renamed_question")],
)
async def test_a_misconfigured_check_is_errored_and_never_blocks_the_baseline(
    tmp_path: Path, decision_set: str, rubric: str
) -> None:
    """JevCheck doesn't gate: even a broken check leaves the judge's baseline written."""
    dataset = Dataset[EvalCaseInputs, str, object](
        name="sample",
        cases=[
            Case(
                name="hello", inputs=EvalCaseInputs(prompt="hi", deps={"name": "A", "sport": "gym"})
            )
        ],
        evaluators=[JevCheck(decision_set=decision_set, rubric=rubric, threshold=0.5)],
    )
    (tmp_path / "datasets").mkdir()
    dataset.to_file(tmp_path / "datasets" / "sample.yaml", custom_evaluator_types=[JevCheck])

    result = await _run(tmp_path, FakeJev({"no_diagnosis": 0.9}))

    label = result.report.cases[0].labels[f"jev:{rubric}"]
    assert label.value == "errored"
    assert decision_set in str(label.reason) or rubric in str(label.reason)
    assert (tmp_path / "baselines" / "sample.json").exists()


async def test_a_check_with_no_paired_judge_on_the_case_is_shown_without_agreement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = Dataset[EvalCaseInputs, str, object](
        name="sample",
        cases=[
            Case(
                name="hello",
                inputs=EvalCaseInputs(prompt="hi", deps={"name": "Alex", "sport": "gym"}),
            )
        ],
        evaluators=[JevCheck(decision_set="rubric", rubric="no_diagnosis", threshold=0.5)],
    )
    (tmp_path / "datasets").mkdir()
    dataset.to_file(tmp_path / "datasets" / "sample.yaml", custom_evaluator_types=[JevCheck])

    await _run(tmp_path, FakeJev({"no_diagnosis": 0.9}))

    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if "hello" in line and "no judge" in line)
    assert "no_diagnosis" in row
    assert "agreement: none" in out


async def test_a_run_without_jev_checks_prints_no_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_dataset(tmp_path / "datasets", with_jev=False)

    result = await _run(tmp_path, FakeJev({}))

    assert "JevCheck vs LLMJudge" not in capsys.readouterr().out
    report = json.loads((tmp_path / "reports" / "sample.json").read_text())
    assert not {"jev_checks", "jev_agreement", "jev_agreement_by_rubric", "jev_errored"} & set(
        report
    )
    # Other fields keep their old shape, nulls included.
    assert "averages" in report
    assert result.baseline.assertions == 1.0


async def test_a_check_before_configuration_says_how_to_fix_it() -> None:
    # The autouse fixture in conftest leaves no Jev configured at the start of each test.
    check = JevCheck(decision_set="rubric", rubric="no_diagnosis", threshold=0.5)

    with pytest.raises(RuntimeError, match="configure_jev_check"):
        check.require_configured()


@dataclass
class _Mood(Evaluator[EvalCaseInputs, object, object]):
    """Another evaluator that labels: its label is not a Jev check."""

    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, object, object]) -> str:
        return "cheerful"


async def test_labels_from_other_evaluators_are_not_reported_as_jev_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = Dataset[EvalCaseInputs, str, object](
        name="sample",
        cases=[
            Case(
                name="hello", inputs=EvalCaseInputs(prompt="hi", deps={"name": "A", "sport": "gym"})
            )
        ],
        evaluators=[_Mood(), JevCheck(decision_set="rubric", rubric="no_diagnosis", threshold=0.5)],
    )
    (tmp_path / "datasets").mkdir()
    dataset.to_file(tmp_path / "datasets" / "sample.yaml", custom_evaluator_types=[JevCheck, _Mood])
    configure_jev_check(FakeJev({"no_diagnosis": 0.9}), loader=DecisionSetLoader(root=FIXTURES))
    registry = PromptRegistry(root=_FIXTURES_ROOT)
    registry.register(_TEXT_TEMPLATE)

    await run_eval(
        registry=registry,
        template_id="sample",
        version=1,
        model=FunctionModel(_reply),
        model_id="test/under-test",
        judge_model=_judge(True),
        judge_model_id="test/judge",
        datasets_root=tmp_path / "datasets",
        baselines_root=tmp_path / "baselines",
        reports_root=tmp_path / "reports",
        custom_evaluator_types=[JevCheck, _Mood],
    )

    report = json.loads((tmp_path / "reports" / "sample.json").read_text())
    assert [check["rubric"] for check in report["jev_checks"]] == ["no_diagnosis"]
