"""The `picture_brief_check` dataset, smoke set and smoke runner (ADR-0020 invariant 6, ADR-0034,
#642). The real separation at the thresholds is a run on Jev in phase C; here the dataset is proven
to hold what the app sends, and the runner to grade with the app's own `check_passes` at each
question's threshold, on a fake Jev."""

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

from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals.picture_brief_check_smoke import _read, run_picture_brief_check_smoke
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.evals.smoke import SmokeError, read_smoke_set
from ai_trainer.llm.picture_brief_check import (
    PICTURE_BRIEF_CHECK_ID,
    PICTURE_BRIEF_CHECK_QUESTIONS,
    PICTURE_BRIEF_CHECK_VERSION,
    BriefCheckState,
    BriefCheckThresholds,
)
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.settings import Settings

_REPO = Path(__file__).parents[2]
_DATASET = _REPO / "evals" / "datasets" / f"{PICTURE_BRIEF_CHECK_ID}.yaml"
_SMOKE = _REPO / "evals" / "smoke"
# Each kind's labels: (same_movement, keeps_rules).
_KINDS = {
    "good": (True, True),
    "another_movement": (False, True),
    "labels": (True, False),
    "two_figures": (True, False),
    "no_positions": (True, False),
}
_MINIMUM = {"good": 8, "another_movement": 4, "labels": 3, "two_figures": 2, "no_positions": 2}
_THRESHOLDS = BriefCheckThresholds(same_movement=0.5, keeps_rules=0.5)


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
    brief = yaml.safe_load((_SMOKE / "media.picture_brief.yaml").read_text())
    howto = yaml.safe_load((_SMOKE / "exercise.simple_howto.yaml").read_text())
    return {
        case["inputs"]["deps"]["description"]
        for case in brief["cases"]
        if case["inputs"]["deps"]["description"]
    } | {case["inputs"]["deps"]["instructions"] for case in howto["cases"]}


def test_dataset_has_at_least_twenty_labelled_cases_of_every_kind() -> None:
    cases = _cases()

    assert len(cases) >= 20
    counts = {kind: 0 for kind in _KINDS}
    for case in cases:
        kind = _kind(case)
        counts[kind] += 1
        labels = _labels(case)
        assert tuple(labels) == PICTURE_BRIEF_CHECK_QUESTIONS, case.name
        assert (labels["same_movement"], labels["keeps_rules"]) == _KINDS[kind], case.name
    for kind, minimum in _MINIMUM.items():
        assert counts[kind] >= minimum, kind


def test_dataset_prompt_and_deps_stay_in_step_with_the_app_state() -> None:
    instructions = _catalogue_instructions()
    for case in _cases():
        state = BriefCheckState.model_validate(case.inputs.deps)
        assert case.inputs.prompt == state.model_dump_json(), case.name
        assert state.instructions in instructions, case.name


def test_smoke_set_is_cut_from_the_dataset_with_one_case_of_every_kind() -> None:
    by_name = {case.name: case for case in _cases()}
    smoke_set = read_smoke_set(PICTURE_BRIEF_CHECK_ID, smoke_root=_SMOKE)
    cases = _read(smoke_set)

    assert len(cases) >= 6
    assert not smoke_set.has_judge
    kinds = set()
    for case in cases:
        source = by_name[case.name]
        assert json.loads(source.inputs.prompt) == case.inputs.deps.model_dump(), case.name
        assert case.expected_output.passes == all(_labels(source).values()), case.name
        kinds.add(_kind(source))
    assert kinds == set(_KINDS)


def test_the_full_run_grades_each_question_at_its_own_settings_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PICTURE_BRIEF_CHECK_MOVEMENT_THRESHOLD", "0.23")
    monkeypatch.setenv("PICTURE_BRIEF_CHECK_RULES_THRESHOLD", "0.73")

    assert cli_module._decision_thresholds(PICTURE_BRIEF_CHECK_ID, Settings()) == {
        "same_movement": 0.23,
        "keeps_rules": 0.73,
    }


def test_the_full_run_measures_the_apps_version() -> None:
    versions = cli_module._active_versions(default_sport_registry())

    assert versions[PICTURE_BRIEF_CHECK_ID] == PICTURE_BRIEF_CHECK_VERSION


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
    (tmp_path / f"{PICTURE_BRIEF_CHECK_ID}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": PICTURE_BRIEF_CHECK_ID,
                "cases": [
                    {
                        "name": "pull-up",
                        "inputs": {
                            "prompt": "Pull-Up",
                            "deps": {"instructions": "Hang, then pull.", "brief": "One person."},
                        },
                        "expected_output": {"passes": passes},
                    }
                ],
            }
        )
    )
    return tmp_path


@pytest.mark.usefixtures("_allow_fake_decisions")
@pytest.mark.parametrize(
    ("same_movement", "keeps_rules", "passes", "graded_pass"),
    [
        (0.5, 0.5, True, True),
        (0.49, 0.99, True, False),
        (0.99, 0.49, False, True),
        (0.05, 0.05, False, True),
        (0.99, 0.99, False, False),
    ],
    ids=["good_at_thresholds", "good_dropped", "broken_caught", "both_caught", "broken_missed"],
)
async def test_runner_grades_with_check_passes_at_thresholds(
    tmp_path: Path, same_movement: float, keeps_rules: float, passes: bool, graded_pass: bool
) -> None:
    smoke_set = read_smoke_set(
        PICTURE_BRIEF_CHECK_ID, smoke_root=_smoke_file(tmp_path, passes=passes)
    )
    jev = _Jev({"same_movement": same_movement, "keeps_rules": keeps_rules})

    (result,) = await run_picture_brief_check_smoke(smoke_set, jev, thresholds=_THRESHOLDS)

    assert result.passed is graded_pass
    assert json.loads(str(jev.states[0])) == {
        "instructions": "Hang, then pull.",
        "brief": "One person.",
    }


@pytest.mark.usefixtures("_allow_fake_decisions")
@pytest.mark.parametrize(
    ("same_movement", "keeps_rules", "graded_pass"),
    [(0.6, 0.6, True), (0.6, 0.55, False), (0.25, 0.6, False)],
    ids=["both_above", "rules_below_its_own", "movement_below_its_own"],
)
async def test_the_cli_smoke_runner_reads_both_settings_thresholds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    same_movement: float,
    keeps_rules: float,
    graded_pass: bool,
) -> None:
    monkeypatch.setenv("PICTURE_BRIEF_CHECK_MOVEMENT_THRESHOLD", "0.3")
    monkeypatch.setenv("PICTURE_BRIEF_CHECK_RULES_THRESHOLD", "0.58")
    decision_runners, _ = cli_module._own_runners(
        Settings(), PromptRegistry(root=_REPO), {PICTURE_BRIEF_CHECK_ID: 1}, None
    )
    smoke_set = read_smoke_set(
        PICTURE_BRIEF_CHECK_ID, smoke_root=_smoke_file(tmp_path, passes=True)
    )

    (result,) = await decision_runners[PICTURE_BRIEF_CHECK_ID](
        smoke_set, _Jev({"same_movement": same_movement, "keeps_rules": keeps_rules})
    )

    assert result.passed is graded_pass


@pytest.mark.usefixtures("_allow_fake_decisions")
async def test_a_failed_call_fails_its_case_alone(tmp_path: Path) -> None:
    smoke_set = read_smoke_set(
        PICTURE_BRIEF_CHECK_ID, smoke_root=_smoke_file(tmp_path, passes=True)
    )

    (result,) = await run_picture_brief_check_smoke(
        smoke_set, _Jev({}, fail=True), thresholds=_THRESHOLDS
    )

    assert not result.passed
    assert "the call failed" in result.detail


def test_a_case_with_no_label_is_refused_before_any_call(tmp_path: Path) -> None:
    (tmp_path / f"{PICTURE_BRIEF_CHECK_ID}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": PICTURE_BRIEF_CHECK_ID,
                "cases": [
                    {
                        "name": "pull-up",
                        "inputs": {"prompt": "x", "deps": {"instructions": "a", "brief": "b"}},
                    }
                ],
            }
        )
    )
    smoke_set = read_smoke_set(PICTURE_BRIEF_CHECK_ID, smoke_root=tmp_path)

    with pytest.raises(SmokeError):
        _read(smoke_set)
