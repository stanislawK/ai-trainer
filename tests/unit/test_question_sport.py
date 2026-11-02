"""`JevQuestionSport` (ADR-0008 amendment at #421's plan gate): it reads Jev's distribution at the
threshold from `Settings`, and every other outcome is no sport, so the question gets "not yet"."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import BaseModel
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

from ai_trainer.application.ports.decision_gateway import DecisionResult
from ai_trainer.application.ports.question_sport import NO_QUESTION_SPORT
from ai_trainer.domain.llm_calls import LlmCallOutcome
from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals.cli import main
from ai_trainer.llm.evals.question_sport_smoke import _read, run_question_sport_smoke
from ai_trainer.llm.evals.smoke import SmokeError, read_smoke_set
from ai_trainer.llm.question_sport import (
    NO_SPORT,
    QUESTION_SPORT_ID,
    QUESTION_SPORT_VERSION,
    JevQuestionSport,
    question_sport_set,
)

_USER = uuid4()
_THRESHOLD = 0.7
_REPO_SMOKE = Path(__file__).parents[2] / "evals" / "smoke"


class _Decisions:
    def __init__(
        self,
        picked: str | None = "gym",
        probability: float = 0.9,
        outcome: LlmCallOutcome = LlmCallOutcome.SUCCESS,
        raises: bool = False,
    ) -> None:
        self._picked = picked
        self._probability = probability
        self._outcome = outcome
        self._raises = raises
        self.calls: list[dict[str, object]] = []

    async def decide[OutputT: BaseModel](
        self,
        *,
        user_id: UUID,
        decision_id: str,
        decision_version: int,
        output_type: type[OutputT],
        state: str,
    ) -> DecisionResult[OutputT]:
        self.calls.append(
            {
                "user_id": user_id,
                "decision_id": decision_id,
                "decision_version": decision_version,
                "state": state,
            }
        )
        if self._raises:
            raise RuntimeError("the row write failed")
        if self._outcome is not LlmCallOutcome.SUCCESS or self._picked is None:
            return DecisionResult(output=None, outcome=self._outcome)
        return DecisionResult(
            output=output_type.model_validate({"sport": self._picked}),
            outcome=self._outcome,
            probabilities={"sport": {self._picked: self._probability}},
        )


def _screen(decisions: _Decisions) -> JevQuestionSport:
    return JevQuestionSport(decisions, threshold=_THRESHOLD)


async def test_a_confident_sport_is_read_from_the_bare_message() -> None:
    decisions = _Decisions()

    assert await _screen(decisions).sport_of(_USER, "How is my bench going?") == "gym"
    assert decisions.calls == [
        {
            "user_id": _USER,
            "decision_id": QUESTION_SPORT_ID,
            "decision_version": QUESTION_SPORT_VERSION,
            "state": "How is my bench going?",
        }
    ]


async def test_a_pick_at_the_threshold_counts() -> None:
    decisions = _Decisions(probability=_THRESHOLD)

    assert await _screen(decisions).sport_of(_USER, "bench?") == "gym"


async def test_a_pick_below_the_threshold_is_no_sport() -> None:
    decisions = _Decisions(probability=_THRESHOLD - 0.01)

    assert await _screen(decisions).sport_of(_USER, "bench?") is None


async def test_no_sport_is_no_sport_however_sure_jev_is() -> None:
    decisions = _Decisions(picked=NO_SPORT, probability=0.99)

    assert await _screen(decisions).sport_of(_USER, "How was my week?") is None


@pytest.mark.parametrize(
    "outcome", [LlmCallOutcome.TIMEOUT, LlmCallOutcome.ERROR, LlmCallOutcome.RATE_LIMITED]
)
async def test_a_failed_call_is_no_sport(outcome: LlmCallOutcome) -> None:
    assert await _screen(_Decisions(outcome=outcome)).sport_of(_USER, "bench?") is None


async def test_a_raising_gateway_is_no_sport() -> None:
    assert await _screen(_Decisions(raises=True)).sport_of(_USER, "bench?") is None


def test_the_set_offers_every_registered_sport_and_no_sport() -> None:
    schema = question_sport_set().output_type.model_json_schema()

    text = str(schema)
    for plugin in default_sport_registry().all():
        assert plugin.id in text
    assert NO_SPORT in text


# --- the smoke runner ----------------------------------------------------------------------


class _Jev(DecisionModel[None]):
    """Picks `sport` at `probability` for every case, or fails."""

    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, sport: str, probability: float, *, fail: bool = False) -> None:
        super().__init__()
        self._sport = sport
        self._probability = probability
        self._fail = fail

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
        answers: dict[str, DecisionAnswer] = {
            "sport": ChoiceAnswer(
                choice=self._sport,
                confidence=self._probability,
                probabilities={self._sport: self._probability},
            )
        }
        return DecisionResponse(
            answers=answers, model_name=self.model_name, usage=RequestUsage(input_tokens=200)
        )


@pytest.fixture
def _allow_fake_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


def _smoke_file(tmp_path: Path, wanted: str = "gym") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "question_sport.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "question_sport",
                "cases": [
                    {
                        "name": "bench",
                        "inputs": {"prompt": "How is my bench going?", "deps": {}},
                        "expected_output": {"sport": wanted},
                    }
                ],
            }
        )
    )
    return tmp_path


@pytest.mark.usefixtures("_allow_fake_decisions")
async def test_the_smoke_set_passes_on_what_the_app_would_read(tmp_path: Path) -> None:
    root = _smoke_file(tmp_path)
    smoke_set = read_smoke_set("question_sport", smoke_root=root)

    (result,) = await run_question_sport_smoke(smoke_set, _Jev("gym", 0.9), threshold=_THRESHOLD)

    assert result.passed


@pytest.mark.usefixtures("_allow_fake_decisions")
async def test_the_smoke_set_fails_a_pick_the_app_would_not_trust(tmp_path: Path) -> None:
    root = _smoke_file(tmp_path)
    smoke_set = read_smoke_set("question_sport", smoke_root=root)

    (result,) = await run_question_sport_smoke(smoke_set, _Jev("gym", 0.5), threshold=_THRESHOLD)

    assert not result.passed
    assert "'none'" in result.detail


@pytest.mark.usefixtures("_allow_fake_decisions")
async def test_a_failed_call_fails_its_case_alone(tmp_path: Path) -> None:
    root = _smoke_file(tmp_path)
    smoke_set = read_smoke_set("question_sport", smoke_root=root)

    (result,) = await run_question_sport_smoke(
        smoke_set, _Jev("gym", 0.9, fail=True), threshold=_THRESHOLD
    )

    assert not result.passed
    assert "the call failed" in result.detail


def test_a_case_with_no_label_is_refused_before_any_call(tmp_path: Path) -> None:
    (tmp_path / "question_sport.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "question_sport",
                "cases": [{"name": "bench", "inputs": {"prompt": "bench?", "deps": {}}}],
            }
        )
    )
    smoke_set = read_smoke_set("question_sport", smoke_root=tmp_path)

    with pytest.raises(SmokeError):
        _read(smoke_set)


async def test_a_reply_service_built_without_a_reader_reads_no_sport() -> None:
    assert await NO_QUESTION_SPORT.sport_of(_USER, "How is my bench going?") is None


def test_main_runs_the_set_on_the_decision_model_and_never_reaches_a_real_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge/model")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    monkeypatch.setattr(cli_module, "_EVALS_ROOT", tmp_path)
    _smoke_file(tmp_path / "smoke")

    assert main(["smoke", "question_sport"]) == 1

    out = capsys.readouterr().out
    assert "question_sport v1 (typesafe/jev-1.13)" in out
    assert "FAIL bench: the call failed" in out


def test_the_committed_smoke_set_has_five_labelled_cases_and_the_abstain_case() -> None:
    smoke_set = read_smoke_set(QUESTION_SPORT_ID, smoke_root=_REPO_SMOKE)
    raw = yaml.safe_load((_REPO_SMOKE / "question_sport.yaml").read_text())

    assert len(smoke_set.cases) >= 5
    labels = {case["expected_output"]["sport"] for case in raw["cases"]}
    assert labels >= {"gym", "climbing", "cycling", NO_SPORT}
