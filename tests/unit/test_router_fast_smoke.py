"""The `router_fast` smoke set (ADR-0034, ticket #436): `evals/smoke/router_fast.yaml` holds
messages labelled with the intents the fast path should take, or `null` when the LLM router should
decide. Each is graded on the route the app would take at the `Settings` thresholds. A fake
`DecisionModel` stands in for Jev (ADR-0013)."""

from pathlib import Path

import pytest
import yaml
from pydantic_ai import models

from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals.cli import main
from ai_trainer.llm.evals.router_fast_smoke import run_router_fast_smoke
from ai_trainer.llm.evals.smoke import SmokeCaseResult, SmokeError, read_smoke_set
from ai_trainer.llm.router_fast import ROUTER_FAST_ID, ROUTER_FAST_VERSION, RouterFastThresholds
from tests.unit.test_eval_router_fast import Answer, ScriptedJev

ROOT = Path(__file__).resolve().parents[2]
SMOKE_ROOT = ROOT / "evals" / "smoke"
THRESHOLDS = RouterFastThresholds(multi_request=0.3, intent=0.85, sport=0.9, plausible_floor=0.1)
_LOG = {"intent": ("log_session", 0.99), "sport": ("gym", 0.99)}
_GYM_LOG = [{"kind": "log_session", "sport": "gym"}]
_MIN_CASES = 5
_MAX_CASES = 10


@pytest.fixture(autouse=True)
def _allow_fake_decision_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


def _case(
    message: str,
    intents: list[dict[str, str]] | None,
    *,
    sports: tuple[str, ...] = ("gym", "cycling"),
) -> dict[str, object]:
    return {
        "name": message,
        "inputs": {"prompt": message, "deps": {"athlete_sports": list(sports)}},
        "expected_output": {"intents": intents},
    }


def _set(root: Path, *cases: dict[str, object]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{ROUTER_FAST_ID}.yaml").write_text(
        yaml.safe_dump({"name": ROUTER_FAST_ID, "cases": list(cases)}, sort_keys=False)
    )
    return root


async def _run(root: Path, answers: dict[str, Answer]) -> list[SmokeCaseResult]:
    smoke_set = read_smoke_set(
        ROUTER_FAST_ID, smoke_root=root, known_templates={ROUTER_FAST_ID: ROUTER_FAST_VERSION}
    )
    return await run_router_fast_smoke(smoke_set, ScriptedJev(answers), thresholds=THRESHOLDS)


async def test_router_fast_smoke_grades_the_route_the_app_would_take(tmp_path: Path) -> None:
    root = _set(
        tmp_path,
        _case("two lifts", _GYM_LOG),
        _case("a lift and a question", None),
    )

    results = await _run(
        root,
        {
            "two lifts": (0.18, _LOG),
            "a lift and a question": (0.58, {**_LOG, "intent": ("ask_training_question", 0.86)}),
        },
    )

    assert [(result.name, result.passed) for result in results] == [
        ("two lifts", True),
        ("a lift and a question", True),
    ]


async def test_a_log_the_router_would_have_to_decide_fails_a_taken_label(tmp_path: Path) -> None:
    results = await _run(_set(tmp_path, _case("two lifts", _GYM_LOG)), {"two lifts": (0.31, _LOG)})

    [result] = results
    assert not result.passed
    assert "the LLM router" in result.detail


async def test_a_message_the_fast_path_takes_fails_a_router_label(tmp_path: Path) -> None:
    """The safety side: pain with a log must not be taken as the pain alone."""
    pain = {**_LOG, "intent": ("wellbeing_or_injury", 0.99)}

    results = await _run(
        _set(tmp_path, _case("squat, knee hurts", None)), {"squat, knee hurts": (0.2, pain)}
    )

    [result] = results
    assert not result.passed
    assert "wellbeing_or_injury" in result.detail


async def test_a_sure_log_with_a_split_sport_fails_a_router_label(tmp_path: Path) -> None:
    """`route_message` gives such a log the conversation's sport or a sport question, so a list
    of two sports' activities would lose one of them; it never reaches the LLM router."""
    split = {"intent": ("log_session", 0.99), "sport": ("gym", 0.6)}
    root = _set(tmp_path, _case("bench, squat and 3 boulders", None))

    results = await _run(root, {"bench, squat and 3 boulders": (0.2, split)})

    [result] = results
    assert not result.passed
    assert "one log, its sport asked" in result.detail


async def test_a_log_routed_to_the_wrong_sport_fails(tmp_path: Path) -> None:
    root = _set(tmp_path, _case("two lifts", _GYM_LOG))

    results = await _run(root, {"two lifts": (0.1, {**_LOG, "sport": ("cycling", 0.99)})})

    assert not results[0].passed


async def test_a_single_sport_athletes_log_is_routed_with_no_sport_question(tmp_path: Path) -> None:
    root = _set(tmp_path, _case("two lifts", _GYM_LOG, sports=("gym",)))

    results = await _run(root, {"two lifts": (0.18, {"intent": ("log_session", 0.99)})})

    assert results[0].passed


async def test_a_failing_call_fails_its_case_and_the_rest_still_run(tmp_path: Path) -> None:
    root = _set(tmp_path, _case("unscripted", _GYM_LOG), _case("two lifts", _GYM_LOG))

    results = await _run(root, {"two lifts": (0.18, _LOG)})

    assert [result.passed for result in results] == [False, True]
    assert "the call failed" in results[0].detail


@pytest.mark.parametrize(
    "broken",
    [
        {"name": "x", "inputs": {"prompt": "x", "deps": {}}, "expected_output": {"intents": None}},
        {
            "name": "x",
            "inputs": {"prompt": "x", "deps": {"athlete_sports": ["gym"]}},
            "expected_output": {},
        },
        {
            "name": "x",
            "inputs": {"prompt": "x", "deps": {"athlete_sports": ["gym"]}},
            "expected_output": {"intents": [{"kind": "not_a_kind"}]},
        },
    ],
    ids=["no sports", "no label", "unknown kind"],
)
async def test_a_malformed_case_is_refused_before_any_call(
    tmp_path: Path, broken: dict[str, object]
) -> None:
    jev = ScriptedJev({})
    smoke_set = read_smoke_set(
        ROUTER_FAST_ID,
        smoke_root=_set(tmp_path, broken),
        known_templates={ROUTER_FAST_ID: ROUTER_FAST_VERSION},
    )

    with pytest.raises(SmokeError, match=ROUTER_FAST_ID):
        await run_router_fast_smoke(smoke_set, jev, thresholds=THRESHOLDS)

    assert jev.offered == {}


def test_main_runs_the_set_on_the_decision_model_and_never_reaches_a_real_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The wiring ran (settings, key, `TypeSafeModel`); the no-model-requests guard stops the
    call, which fails its case rather than reaching OpenRouter (ADR-0013)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge/model")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    monkeypatch.setattr(cli_module, "_EVALS_ROOT", tmp_path)
    _set(tmp_path / "smoke", _case("two lifts", _GYM_LOG))

    assert main(["smoke", ROUTER_FAST_ID, "--model", "typesafe/jev-1.13"]) == 1

    out = capsys.readouterr().out
    assert f"{ROUTER_FAST_ID} v{ROUTER_FAST_VERSION} (typesafe/jev-1.13)" in out
    assert "FAIL two lifts: the call failed" in out


# --- the committed set -------------------------------------------------------------------------


def test_committed_router_fast_smoke_set_reads() -> None:
    smoke_set = read_smoke_set(
        ROUTER_FAST_ID, smoke_root=SMOKE_ROOT, known_templates={ROUTER_FAST_ID: ROUTER_FAST_VERSION}
    )
    cases = yaml.safe_load((SMOKE_ROOT / f"{ROUTER_FAST_ID}.yaml").read_text())["cases"]
    labels = [case["expected_output"]["intents"] for case in cases]

    assert _MIN_CASES <= len(smoke_set.cases) <= _MAX_CASES
    assert smoke_set.known_gaps == {}
    assert not smoke_set.has_judge
    # Both sides of the threshold are guarded: taken logs, and messages left to the router.
    assert None in labels
    assert _GYM_LOG in labels
    # Real chats first (ADR-0034).
    assert any((case.metadata or {}).get("field") for case in smoke_set.cases)
    # Europe first (G15).
    assert any("przysiady" in case.inputs.prompt for case in smoke_set.cases)
