"""The `goal_kind` dataset, smoke set and smoke runner (ADR-0020 invariant 6, ADR-0034, #625). The
real separation at the threshold is a run on Jev in phase C; here the dataset is proven to hold
what the app sends, and the runner to grade with the app's own `confident_kind` at
`goal_kind_threshold`, on a fake Jev."""

import json
from pathlib import Path

import pytest
import yaml
from pydantic_ai import models
from pydantic_ai.models.decision import (
    ChoiceAnswer,
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
)
from pydantic_ai.usage import RequestUsage
from pydantic_evals import Case, Dataset

from ai_trainer.domain.goals import GoalKind
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals.goal_kind_smoke import _read, run_goal_kind_smoke
from ai_trainer.llm.evals.runner import EvalCaseInputs
from ai_trainer.llm.evals.smoke import SmokeError, read_smoke_set
from ai_trainer.llm.goal_kind import GOAL_KIND_ID, GOAL_KIND_VERSION, NO_KIND, GoalKindState
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.settings import Settings

_REPO = Path(__file__).parents[2]
_DATASET = _REPO / "evals" / "datasets" / f"{GOAL_KIND_ID}.yaml"
_SMOKE = _REPO / "evals" / "smoke"
_THRESHOLD = 0.7
_LABELS = {kind.value for kind in GoalKind} | {NO_KIND}
# The ticket's three ACs, which the smoke set must carry.
_AC_CASES = {
    "Touch my toes by summer": "mobility",
    "Lose 5 kg": "weight_loss",
    "Deadlift 150 kg": "strength",
}


def _cases() -> list[Case[EvalCaseInputs, object, object]]:
    return list(Dataset[EvalCaseInputs, object, object].from_file(_DATASET).cases)


def _label(case: Case[EvalCaseInputs, object, object]) -> str:
    assert isinstance(case.expected_output, dict), case.name
    label = case.expected_output["kind"]
    assert isinstance(label, str)
    return label


def test_dataset_has_two_cases_of_every_kind_and_the_none_case() -> None:
    cases = _cases()

    assert len(cases) >= 14
    counts = dict.fromkeys(_LABELS, 0)
    for case in cases:
        counts[_label(case)] += 1
    assert all(count >= 2 for kind, count in counts.items() if kind != NO_KIND), counts
    assert counts[NO_KIND] >= 1


def test_dataset_prompt_and_deps_stay_in_step_with_the_app_state() -> None:
    for case in _cases():
        state = GoalKindState.model_validate(case.inputs.deps)
        assert case.inputs.prompt == state.model_dump_json(), case.name


def test_smoke_set_is_cut_from_the_dataset_with_the_acs_a_near_tie_and_none() -> None:
    by_name = {case.name: case for case in _cases()}
    smoke_set = read_smoke_set(GOAL_KIND_ID, smoke_root=_SMOKE)
    cases = _read(smoke_set)

    assert len(cases) >= 6
    assert not smoke_set.has_judge
    for case in cases:
        source = by_name[case.name]
        assert json.loads(source.inputs.prompt) == case.inputs.deps.model_dump(), case.name
        assert case.expected_output.kind == _label(source), case.name
    picked = {case.inputs.deps.goal: case.expected_output.kind for case in cases}
    assert _AC_CASES.items() <= picked.items()
    assert {NO_KIND, "endurance"} <= set(picked.values())
    metadata = [by_name[case.name].metadata for case in cases]
    assert any(isinstance(meta, dict) and meta.get("near_tie") for meta in metadata)


def test_the_full_run_measures_the_apps_version() -> None:
    versions = cli_module._active_versions(default_sport_registry())

    assert versions[GOAL_KIND_ID] == GOAL_KIND_VERSION


class _Jev(DecisionModel[None]):
    """Picks `kind` at `probability` for every case, or fails."""

    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, kind: str, probability: float, *, fail: bool = False) -> None:
        super().__init__()
        self._kind = kind
        self._probability = probability
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
            "kind": ChoiceAnswer(
                choice=self._kind,
                confidence=self._probability,
                probabilities={self._kind: self._probability},
            )
        }
        return DecisionResponse(
            answers=answers, model_name=self.model_name, usage=RequestUsage(input_tokens=80)
        )


@pytest.fixture
def _allow_fake_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


def _smoke_file(tmp_path: Path, *, wanted: str) -> Path:
    (tmp_path / f"{GOAL_KIND_ID}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": GOAL_KIND_ID,
                "cases": [
                    {
                        "name": "toes",
                        "inputs": {
                            "prompt": "Touch my toes by summer",
                            "deps": {"goal": "Touch my toes by summer"},
                        },
                        "expected_output": {"kind": wanted},
                    }
                ],
            }
        )
    )
    return tmp_path


@pytest.mark.usefixtures("_allow_fake_decisions")
@pytest.mark.parametrize(
    ("picked", "probability", "wanted", "graded_pass"),
    [
        ("mobility", 0.7, "mobility", True),
        ("mobility", 0.69, "mobility", False),
        ("mobility", 0.69, NO_KIND, True),
        (NO_KIND, 0.99, NO_KIND, True),
        ("strength", 0.95, "mobility", False),
        ("mobility", 0.95, NO_KIND, False),
    ],
    ids=[
        "confident_pick",
        "pick_below_threshold",
        "none_wanted_below_threshold",
        "none_picked",
        "wrong_kind",
        "kind_where_none_wanted",
    ],
)
async def test_runner_grades_with_confident_kind_at_the_threshold(
    tmp_path: Path, picked: str, probability: float, wanted: str, graded_pass: bool
) -> None:
    smoke_set = read_smoke_set(GOAL_KIND_ID, smoke_root=_smoke_file(tmp_path, wanted=wanted))
    jev = _Jev(picked, probability)

    (result,) = await run_goal_kind_smoke(smoke_set, jev, threshold=_THRESHOLD)

    assert result.passed is graded_pass
    assert json.loads(str(jev.states[0])) == {"goal": "Touch my toes by summer"}


@pytest.mark.usefixtures("_allow_fake_decisions")
async def test_the_cli_smoke_runner_reads_the_settings_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOAL_KIND_THRESHOLD", "0.5")
    decision_runners, _ = cli_module._own_runners(
        Settings(), PromptRegistry(root=_REPO), {GOAL_KIND_ID: 1}, None
    )
    smoke_set = read_smoke_set(GOAL_KIND_ID, smoke_root=_smoke_file(tmp_path, wanted="mobility"))

    (result,) = await decision_runners[GOAL_KIND_ID](smoke_set, _Jev("mobility", 0.55))

    assert result.passed


@pytest.mark.usefixtures("_allow_fake_decisions")
async def test_a_failed_call_fails_its_case_alone(tmp_path: Path) -> None:
    smoke_set = read_smoke_set(GOAL_KIND_ID, smoke_root=_smoke_file(tmp_path, wanted="mobility"))

    (result,) = await run_goal_kind_smoke(
        smoke_set, _Jev("mobility", 0.9, fail=True), threshold=_THRESHOLD
    )

    assert not result.passed
    assert "the call failed" in result.detail


def test_a_case_with_no_label_is_refused_before_any_call(tmp_path: Path) -> None:
    (tmp_path / f"{GOAL_KIND_ID}.yaml").write_text(
        yaml.safe_dump(
            {
                "name": GOAL_KIND_ID,
                "cases": [{"name": "toes", "inputs": {"prompt": "x", "deps": {"goal": "x"}}}],
            }
        )
    )
    smoke_set = read_smoke_set(GOAL_KIND_ID, smoke_root=tmp_path)

    with pytest.raises(SmokeError):
        _read(smoke_set)
