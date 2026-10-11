"""`router_fast`'s eval (ADR-0009 amendment, ADR-0020, #122): the router dataset's cases, no
copy, each asked of Jev with that case's sports, routed by the app's own `fast_intents`, and
graded with the router's own matching. It reports coverage and accuracy and refuses a fast
path less accurate than the LLM router's baseline. A fake `DecisionModel` stands in for Jev."""

from pathlib import Path

import pytest
import yaml
from pydantic_ai import models
from pydantic_ai.models.decision import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
    NoulAnswer,
)
from pydantic_ai.usage import RequestUsage

from ai_trainer.llm.decisions.loader import DecisionSetLoader
from ai_trainer.llm.evals.baseline import Baseline, save_baseline
from ai_trainer.llm.evals.router_fast import (
    FastPathAccuracyError,
    RouterFastEvalRunResult,
    run_router_fast_eval,
)
from ai_trainer.llm.router_fast import RouterFastThresholds

SNAPSHOT = "typesafe/jev-1.13-20260917"
THRESHOLDS = RouterFastThresholds(multi_request=0.2, intent=0.9, sport=0.9, plausible_floor=0.25)
ALL_SPORTS = ["climbing", "gym", "cycling"]

# Jev's answer per message: P(several requests), then each choice's pick and probability.
type Answer = tuple[float, dict[str, tuple[str, float]]]


class ScriptedJev(DecisionModel[None]):
    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, answers: dict[str, Answer]) -> None:
        super().__init__()
        self._answers = answers
        self.offered: dict[str, dict[str, list[str]]] = {}

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
        state = str(request.state)
        offered = {
            name: list(question.criteria)
            for name, question in request.questions.items()
            if isinstance(question, ChoiceQuestion)
        }
        self.offered[state] = offered
        multi, picks = self._answers[state]
        answers: dict[str, DecisionAnswer] = {"multi_request": NoulAnswer(noul=multi)}
        for name, options in offered.items():
            pick, p = picks[name]
            rest = (1 - p) / (len(options) - 1)
            answers[name] = ChoiceAnswer(
                choice=pick,
                confidence=p,
                probabilities={option: p if option == pick else rest for option in options},
            )
        return DecisionResponse(
            answers=answers, model_name=SNAPSHOT, usage=RequestUsage(input_tokens=200)
        )


@pytest.fixture(autouse=True)
def _allow_fake_decision_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


def _case(
    name: str,
    message: str,
    intents: list[dict[str, object]],
    *,
    sports: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    case: dict[str, object] = {
        "name": name,
        "inputs": {
            "prompt": message,
            "deps": {
                "message": message,
                "history": [],
                "athlete_sports": ALL_SPORTS if sports is None else sports,
                "today": "2026-09-30",
                "now_local": "2026-09-30T18:30:00+02:00",
                "timezone": "Europe/Warsaw",
            },
        },
        "expected_output": {
            "intents": [{"confidence": 0.9, "span": "", **intent} for intent in intents]
        },
    }
    if metadata is not None:
        case["metadata"] = metadata
    return case


_HI = _case("greeting", "hi!", [{"kind": "chitchat"}])
_RIDE = _case("ride", "Rode 45 km", [{"kind": "log_session", "sport": "cycling"}])
_GYM_ONLY = _case(
    "single sport", "Did an hour", [{"kind": "log_session", "sport": "gym"}], sports=["gym"]
)
_MIXED = _case(
    "mixed",
    "Ran 5k, also plan my week",
    [{"kind": "log_session", "sport": "cycling"}, {"kind": "request_plan"}],
)

_CONFIDENT: dict[str, Answer] = {
    "hi!": (0.02, {"intent": ("chitchat", 0.97), "sport": ("gym", 0.4)}),
    "Rode 45 km": (0.03, {"intent": ("log_session", 0.96), "sport": ("cycling", 0.95)}),
    "Did an hour": (0.05, {"intent": ("log_session", 0.93)}),
    "Ran 5k, also plan my week": (
        0.91,
        {"intent": ("log_session", 0.6), "sport": ("cycling", 0.7)},
    ),
}


def _write(root: Path, cases: list[dict[str, object]], *, router_accuracy: float = 0.9) -> None:
    datasets = root / "datasets"
    datasets.mkdir(parents=True, exist_ok=True)
    (datasets / "router.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "router",
                "cases": cases,
                "evaluators": ["IntentKindsMatch", "LogSessionSportMatch"],
            }
        )
    )
    save_baseline(
        root / "baselines" / "router.json",
        Baseline(
            template_id="router",
            version=2,
            model="z-ai/glm-5.3-flash",
            assertions=router_accuracy,
            scores={},
        ),
    )


async def _run(
    root: Path, jev: ScriptedJev, *, repeat: int = 1, thresholds: RouterFastThresholds = THRESHOLDS
) -> RouterFastEvalRunResult:
    return await run_router_fast_eval(
        loader=DecisionSetLoader(),
        version=1,
        model=jev,
        model_id="typesafe/jev-1.13",
        thresholds=thresholds,
        datasets_root=root / "datasets",
        baselines_root=root / "baselines",
        reports_root=root / "reports",
        repeat=repeat,
    )


async def test_the_router_cases_are_graded_with_no_dataset_of_its_own(tmp_path: Path) -> None:
    _write(tmp_path, [_HI, _RIDE, _GYM_ONLY, _MIXED])

    result = await _run(tmp_path, ScriptedJev(_CONFIDENT))

    assert not (tmp_path / "datasets" / "router_fast.yaml").exists()
    assert (result.coverage, result.accuracy) == (0.75, 1.0)


async def test_the_baseline_stores_the_thresholds_coverage_and_accuracy(tmp_path: Path) -> None:
    _write(tmp_path, [_HI, _RIDE, _GYM_ONLY, _MIXED])

    await _run(tmp_path, ScriptedJev(_CONFIDENT))

    saved = Baseline.model_validate_json((tmp_path / "baselines" / "router_fast.json").read_text())
    assert (saved.template_id, saved.version, saved.model) == (
        "router_fast",
        1,
        "typesafe/jev-1.13",
    )
    assert saved.decision_thresholds == {
        "multi_request": 0.2,
        "intent": 0.9,
        "sport": 0.9,
        "plausible_floor": 0.25,
    }
    assert saved.scores == {"coverage": 0.75, "accuracy": 1.0}
    assert saved.assertions == 1.0


async def test_each_case_records_its_probabilities(tmp_path: Path) -> None:
    _write(tmp_path, [_RIDE, _GYM_ONLY])

    result = await _run(tmp_path, ScriptedJev(_CONFIDENT), repeat=2)

    assert result.baseline.cases is not None
    ride = result.baseline.cases["ride"]
    assert (ride["multi_request"].mean, ride["intent"].mean, ride["sport"].mean) == (
        0.03,
        0.96,
        0.95,
    )
    assert ride["intent"].runs == 2
    assert "sport" not in result.baseline.cases["single sport"]


async def test_each_case_offers_only_its_athletes_sports(tmp_path: Path) -> None:
    jev = ScriptedJev(_CONFIDENT)
    _write(tmp_path, [_RIDE, _GYM_ONLY])

    await _run(tmp_path, jev)

    assert jev.offered["Rode 45 km"]["sport"] == ALL_SPORTS
    assert "sport" not in jev.offered["Did an hour"]


async def test_a_several_request_message_is_declined_and_still_passes(tmp_path: Path) -> None:
    _write(tmp_path, [_HI, _MIXED])

    result = await _run(tmp_path, ScriptedJev(_CONFIDENT))

    [mixed] = [case for case in result.report.cases if case.name == "mixed"]
    assert mixed.assertions["fast_route_safe"].value is True
    assert mixed.scores["coverage"].value == 0.0


async def test_a_wrong_fast_route_fails_its_case_and_lowers_accuracy(tmp_path: Path) -> None:
    wrong = dict(_CONFIDENT)
    wrong["Rode 45 km"] = (0.03, {"intent": ("log_session", 0.96), "sport": ("gym", 0.95)})
    _write(tmp_path, [_HI, _RIDE, _GYM_ONLY], router_accuracy=0.5)

    result = await _run(tmp_path, ScriptedJev(wrong))

    [ride] = [case for case in result.report.cases if case.name == "ride"]
    assert ride.assertions["fast_route_safe"].value is False
    assert result.accuracy == pytest.approx(2 / 3)


async def test_a_router_cases_acceptable_sports_are_honoured(tmp_path: Path) -> None:
    ambiguous = _case(
        "ambiguous",
        "Two hours of training",
        [{"kind": "log_session", "sport": "climbing"}],
        metadata={"acceptable_sports": ["climbing", "gym"]},
    )
    answers: dict[str, Answer] = {
        "Two hours of training": (0.05, {"intent": ("log_session", 0.95), "sport": ("gym", 0.95)}),
    }
    _write(tmp_path, [ambiguous])

    result = await _run(tmp_path, ScriptedJev(answers))

    assert (result.coverage, result.accuracy) == (1.0, 1.0)


async def test_a_fast_path_less_accurate_than_the_router_writes_no_baseline(
    tmp_path: Path,
) -> None:
    wrong = dict(_CONFIDENT)
    wrong["hi!"] = (0.02, {"intent": ("ask_training_question", 0.97), "sport": ("gym", 0.4)})
    _write(tmp_path, [_HI, _RIDE, _GYM_ONLY], router_accuracy=0.977)

    with pytest.raises(FastPathAccuracyError, match=r"0\.667 is below the router's 0\.977"):
        await _run(tmp_path, ScriptedJev(wrong))

    assert not (tmp_path / "baselines" / "router_fast.json").exists()


async def test_a_run_that_takes_no_case_writes_no_baseline(tmp_path: Path) -> None:
    _write(tmp_path, [_MIXED])

    with pytest.raises(FastPathAccuracyError, match="took no case"):
        await _run(tmp_path, ScriptedJev(_CONFIDENT))

    assert not (tmp_path / "baselines" / "router_fast.json").exists()


async def test_without_a_router_baseline_there_is_nothing_to_compare_with(tmp_path: Path) -> None:
    _write(tmp_path, [_HI])
    (tmp_path / "baselines" / "router.json").unlink()

    with pytest.raises(FastPathAccuracyError, match="no router baseline"):
        await _run(tmp_path, ScriptedJev(_CONFIDENT))


async def test_a_threshold_change_is_reported(tmp_path: Path) -> None:
    _write(tmp_path, [_HI, _RIDE])
    await _run(tmp_path, ScriptedJev(_CONFIDENT))

    result = await _run(
        tmp_path,
        ScriptedJev(_CONFIDENT),
        thresholds=RouterFastThresholds(
            multi_request=0.2, intent=0.95, sport=0.9, plausible_floor=0.25
        ),
    )

    assert result.changes == ["threshold 'intent' changed from 0.9 to 0.95"]
    assert result.regressions == []


# Plausible sports (ticket #123, B23): graded against `metadata.plausible_sports`.

_BOARD = _case(
    "board",
    "Did 4x4s on the board",
    [{"kind": "log_session", "sport": "climbing"}],
    sports=["climbing", "gym"],
    metadata={"plausible_sports": ["climbing"]},
)
_INTERVALS = _case(
    "intervals",
    "Did intervals",
    [{"kind": "log_session", "sport": "gym"}],
    sports=["gym", "cycling"],
    metadata={"plausible_sports": ["gym", "cycling"], "acceptable_sports": ["gym", "cycling"]},
)
_EASY = _case(
    "easy",
    "2 hours easy",
    [{"kind": "log_session", "sport": "cycling"}],
    sports=["cycling"],
    metadata={"plausible_sports": ["cycling"]},
)
_PLAUSIBLE: dict[str, Answer] = {
    "Did 4x4s on the board": (
        0.04,
        {"intent": ("log_session", 0.96), "sport": ("climbing", 0.93)},
    ),
    "Did intervals": (0.04, {"intent": ("log_session", 0.96), "sport": ("gym", 0.55)}),
    "2 hours easy": (0.04, {"intent": ("log_session", 0.95)}),
    **_CONFIDENT,
}


async def test_plausible_sports_are_graded_as_sets_against_the_cases_metadata(
    tmp_path: Path,
) -> None:
    _write(tmp_path, [_BOARD, _INTERVALS, _EASY])

    result = await _run(tmp_path, ScriptedJev(_PLAUSIBLE))

    assert all(
        case.assertions["plausible_sports_right"].value is True for case in result.report.cases
    )
    assert result.baseline.scores["plausible_accuracy"] == 1.0


async def test_a_split_sport_log_is_declined_yet_its_plausible_sports_are_graded(
    tmp_path: Path,
) -> None:
    _write(tmp_path, [_HI, _BOARD, _INTERVALS])

    result = await _run(tmp_path, ScriptedJev(_PLAUSIBLE))

    [intervals] = [case for case in result.report.cases if case.name == "intervals"]
    assert intervals.scores["coverage"].value == 0.0
    assert intervals.assertions["plausible_sports_right"].value is True


async def test_a_wrong_plausible_set_fails_its_case_and_lowers_the_score(tmp_path: Path) -> None:
    wrong = dict(_PLAUSIBLE)
    wrong["Did 4x4s on the board"] = (
        0.04,
        {"intent": ("log_session", 0.96), "sport": ("climbing", 0.6)},
    )
    _write(tmp_path, [_HI, _BOARD, _INTERVALS])

    result = await _run(tmp_path, ScriptedJev(wrong))

    [board] = [case for case in result.report.cases if case.name == "board"]
    assert board.assertions["plausible_sports_right"].value is False
    assert result.baseline.scores["plausible_accuracy"] == 0.5


async def test_a_case_without_expected_plausible_sports_is_not_graded_on_them(
    tmp_path: Path,
) -> None:
    _write(tmp_path, [_HI, _RIDE])

    result = await _run(tmp_path, ScriptedJev(_CONFIDENT))

    assert all("plausible_sports_right" not in case.assertions for case in result.report.cases)
    assert "plausible_accuracy" not in result.baseline.scores


async def test_the_floor_decides_which_sports_are_plausible(tmp_path: Path) -> None:
    _write(tmp_path, [_HI, _BOARD, _INTERVALS])
    jev = ScriptedJev(_PLAUSIBLE)

    strict = RouterFastThresholds(multi_request=0.2, intent=0.9, sport=0.9, plausible_floor=0.5)
    result = await _run(tmp_path, jev, thresholds=strict)

    # 0.45 for cycling is under a 0.5 floor, so "did intervals" is no longer split.
    [intervals] = [case for case in result.report.cases if case.name == "intervals"]
    assert intervals.assertions["plausible_sports_right"].value is False
