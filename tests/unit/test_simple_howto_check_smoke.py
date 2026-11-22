"""The `exercise_simple_howto_check` dataset, smoke set and smoke runner (ADR-0020 invariant 6,
ADR-0034, #591). The real separation at the threshold (AC 4) is a run on Jev in phase C; here the
runner is proven to grade with the app's own `check_passes` at the threshold, on a fake Jev."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic_ai import models
from pydantic_ai.models.decision import (
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
)
from pydantic_ai.usage import RequestUsage
from pydantic_evals import Case, Dataset

from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.evals.simple_howto_check_smoke import _read, run_simple_howto_check_smoke
from ai_trainer.llm.evals.smoke import SmokeError, read_smoke_set
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.simple_howto_check import (
    SIMPLE_HOWTO_CHECK_ID,
    SIMPLE_HOWTO_CHECK_QUESTIONS,
    HowtoCheckState,
)
from ai_trainer.settings import Settings

_REPO = Path(__file__).parents[2]
_DATASET = _REPO / "evals" / "datasets" / f"{SIMPLE_HOWTO_CHECK_ID}.yaml"
_SMOKE = _REPO / "evals" / "smoke"
_HOWTO_SMOKE = _SMOKE / "exercise.simple_howto.yaml"
_BROKEN = {"dropped_cue", "added_cue", "changed_number", "another_movement"}
_THRESHOLD = 0.8


def _cases() -> list[Case[EvalCaseInputs, object, object]]:
    return list(Dataset[EvalCaseInputs, object, object].from_file(_DATASET).cases)


def _kind(case: Case[EvalCaseInputs, object, object]) -> str:
    assert isinstance(case.metadata, dict), case.name
    kind = case.metadata["kind"]
    assert isinstance(kind, str)
    return kind


def _labels(case: Case[EvalCaseInputs, object, object]) -> dict[str, Any]:
    assert isinstance(case.expected_output, dict), case.name
    return case.expected_output


def _catalogue_instructions() -> set[str]:
    howto = yaml.safe_load(_HOWTO_SMOKE.read_text())
    return {case["inputs"]["deps"]["instructions"] for case in howto["cases"]}


def test_dataset_has_at_least_twenty_labelled_cases_of_every_kind() -> None:
    cases = _cases()

    assert len(cases) >= 20
    assert {_kind(case) for case in cases} == {"good", *_BROKEN}
    for case in cases:
        labels = _labels(case)
        assert set(labels) == set(SIMPLE_HOWTO_CHECK_QUESTIONS), case.name
        passes = all(labels.values())
        assert passes == (_kind(case) == "good"), case.name


def test_dataset_state_is_what_the_app_sends_from_catalogue_originals() -> None:
    originals = _catalogue_instructions()
    for case in _cases():
        state = HowtoCheckState.model_validate(case.inputs.deps)
        assert case.inputs.prompt == state.model_dump_json(), case.name
        assert state.original in originals, case.name


def test_smoke_set_is_cut_from_the_dataset_with_every_broken_kind() -> None:
    by_name = {case.name: case for case in _cases()}
    smoke_set = read_smoke_set(SIMPLE_HOWTO_CHECK_ID, smoke_root=_SMOKE)
    cases = _read(smoke_set)

    assert len(cases) >= 5
    assert not smoke_set.has_judge
    kinds = set()
    for case in cases:
        source = by_name[case.name]
        assert json.loads(source.inputs.prompt) == case.inputs.deps.model_dump(), case.name
        assert case.expected_output.passes == all(_labels(source).values()), case.name
        kinds.add(_kind(source))
    assert kinds == {"good", *_BROKEN}


def test_the_full_run_grades_both_questions_at_the_settings_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXERCISE_SIMPLE_HOWTO_CHECK_THRESHOLD", "0.73")

    assert cli_module._decision_thresholds(SIMPLE_HOWTO_CHECK_ID, Settings()) == {
        "keeps_cues": 0.73,
        "adds_nothing": 0.73,
    }


class _Jev(DecisionModel[None]):
    """Answers every question with its P(yes) from `probabilities`, or fails."""

    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, probabilities: dict[str, float], *, fail: bool = False) -> None:
        super().__init__()
        self._probabilities = probabilities
        self._fail = fail
        self.states: list[object] = []

    @property
    def model_name(self) -> str:
        return "typesafe/jev-1.13"

    @property
    def system(self) -> str:
        return "fake"

    @property
    def base_url(self) -> str:
        return "https://jev.invalid"

    async def decide(
        self, request: DecisionRequest, model_settings: DecisionModelSettings
    ) -> DecisionResponse:
        if self._fail:
            raise RuntimeError("upstream is down")
        self.states.append(request.state)
        answers: dict[str, DecisionAnswer] = {
            name: NoulAnswer(noul=self._probabilities[name]) for name in request.questions
        }
        return DecisionResponse(
            answers=answers, model_name=self.model_name, usage=RequestUsage(input_tokens=200)
        )


@pytest.fixture
def _allow_fake_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


def _smoke_file(tmp_path: Path, *, passes: bool) -> Path:
    (tmp_path / f"{SIMPLE_HOWTO_CHECK_ID}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": SIMPLE_HOWTO_CHECK_ID,
                "cases": [
                    {
                        "name": "bench",
                        "inputs": {
                            "prompt": "Barbell Bench Press",
                            "deps": {"original": "Lower slowly.", "summary": "Lower slowly."},
                        },
                        "expected_output": {"passes": passes},
                    }
                ],
            }
        )
    )
    return tmp_path


# AC 4: graded with the app's `check_passes` at the threshold.
@pytest.mark.usefixtures("_allow_fake_decisions")
@pytest.mark.parametrize(
    ("keeps_cues", "adds_nothing", "passes", "graded_pass"),
    [
        (_THRESHOLD, _THRESHOLD, True, True),
        (_THRESHOLD - 0.01, 0.99, True, False),
        (0.99, _THRESHOLD - 0.01, False, True),
        (0.2, 0.2, False, True),
    ],
    ids=["good_at_threshold", "good_dropped", "broken_caught", "broken_caught_both"],
)
async def test_runner_grades_with_check_passes_at_threshold(
    tmp_path: Path, keeps_cues: float, adds_nothing: float, passes: bool, graded_pass: bool
) -> None:
    smoke_set = read_smoke_set(
        SIMPLE_HOWTO_CHECK_ID, smoke_root=_smoke_file(tmp_path, passes=passes)
    )
    jev = _Jev({"keeps_cues": keeps_cues, "adds_nothing": adds_nothing})

    (result,) = await run_simple_howto_check_smoke(smoke_set, jev, threshold=_THRESHOLD)

    assert result.passed is graded_pass
    assert json.loads(str(jev.states[0])) == {
        "original": "Lower slowly.",
        "summary": "Lower slowly.",
    }


@pytest.mark.usefixtures("_allow_fake_decisions")
async def test_the_cli_smoke_runner_reads_the_settings_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXERCISE_SIMPLE_HOWTO_CHECK_THRESHOLD", "0.5")
    decision_runners, _ = cli_module._own_runners(
        Settings(), PromptRegistry(root=_REPO), {SIMPLE_HOWTO_CHECK_ID: 1}, None
    )
    smoke_set = read_smoke_set(SIMPLE_HOWTO_CHECK_ID, smoke_root=_smoke_file(tmp_path, passes=True))

    (result,) = await decision_runners[SIMPLE_HOWTO_CHECK_ID](
        smoke_set, _Jev({"keeps_cues": 0.6, "adds_nothing": 0.6})
    )

    assert result.passed


@pytest.mark.usefixtures("_allow_fake_decisions")
async def test_a_failed_call_fails_its_case_alone(tmp_path: Path) -> None:
    smoke_set = read_smoke_set(SIMPLE_HOWTO_CHECK_ID, smoke_root=_smoke_file(tmp_path, passes=True))

    (result,) = await run_simple_howto_check_smoke(
        smoke_set, _Jev({}, fail=True), threshold=_THRESHOLD
    )

    assert not result.passed
    assert "the call failed" in result.detail


def test_a_case_with_no_label_is_refused_before_any_call(tmp_path: Path) -> None:
    (tmp_path / f"{SIMPLE_HOWTO_CHECK_ID}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": SIMPLE_HOWTO_CHECK_ID,
                "cases": [
                    {
                        "name": "bench",
                        "inputs": {"prompt": "x", "deps": {"original": "a", "summary": "b"}},
                    }
                ],
            }
        )
    )
    smoke_set = read_smoke_set(SIMPLE_HOWTO_CHECK_ID, smoke_root=tmp_path)

    with pytest.raises(SmokeError):
        _read(smoke_set)
