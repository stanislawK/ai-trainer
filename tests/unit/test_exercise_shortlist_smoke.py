"""The `exercise_shortlist` smoke set (ADR-0034, ticket #399): each case is graded on what the app
would do with Jev's answer at the `Settings` threshold, a confident pick or the card. A fake
`DecisionModel` stands in for Jev (ADR-0013)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic_ai import models
from pydantic_ai.models import Model
from pydantic_ai.models.decision import (
    ChoiceAnswer,
    DecisionAnswer,
    DecisionModel,
    DecisionModelSettings,
    DecisionRequest,
    DecisionResponse,
)
from pydantic_ai.usage import RequestUsage

from ai_trainer import evals_main
from ai_trainer.domain.exercises import ExerciseCandidate
from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals.cli import DecisionSmokeRunner, _build_parser, execute_smoke, main
from ai_trainer.llm.evals.exercise_shortlist_smoke import (
    LiveShortlister,
    run_exercise_shortlist_smoke,
)
from ai_trainer.llm.evals.smoke import SmokeCaseResult, SmokeError, SmokeSet, read_smoke_set
from ai_trainer.llm.exercise_shortlist import EXERCISE_SHORTLIST_ID
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
SMOKE_ROOT = ROOT / "evals" / "smoke"
_MODEL_ID = "typesafe/jev-1.13"
_THRESHOLD = 0.9


class ScriptedJev(DecisionModel[None]):
    """Answers each case by its state (the typed name): the candidate whose description starts
    with the scripted name, at the scripted probability, or "none"."""

    max_choice_options = 255
    max_score_levels = 10

    def __init__(self, answers: dict[str, tuple[str, float]], *, error: Exception | None = None):
        super().__init__()
        self._answers = answers
        self._error = error
        self.states: list[str] = []

    @property
    def model_name(self) -> str:
        return _MODEL_ID

    @property
    def system(self) -> str:
        return "fake"

    @property
    def base_url(self) -> str:
        return "https://jev.invalid"

    async def decide(
        self, request: DecisionRequest, model_settings: DecisionModelSettings
    ) -> DecisionResponse:
        if self._error is not None:
            raise self._error
        state = request.state
        assert isinstance(state, str)
        self.states.append(state)
        wanted, probability = self._answers[state]
        criteria = request.questions["exercise"].criteria
        assert isinstance(criteria, dict)
        picked = (
            "none"
            if wanted == "none"
            else next(
                key
                for key, text in criteria.items()
                if isinstance(text, str) and text.startswith(wanted)
            )
        )
        answers: dict[str, DecisionAnswer] = {
            "exercise": ChoiceAnswer(
                choice=picked,
                confidence=probability,
                probabilities={picked: probability},
            )
        }
        return DecisionResponse(
            answers=answers, model_name=_MODEL_ID, usage=RequestUsage(input_tokens=200)
        )


@pytest.fixture(autouse=True)
def _allow_fake_decision_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


def _set(tmp_path: Path, *cases: dict[str, object]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "exercise_shortlist.yaml").write_text(
        yaml.safe_dump({"name": "exercise_shortlist", "cases": list(cases)}, sort_keys=False)
    )
    return tmp_path


def _case(name: str, pick: str, *candidates: str) -> dict[str, object]:
    return {
        "name": name,
        "inputs": {
            "prompt": name,
            "deps": {"candidates": [{"name": candidate} for candidate in candidates]},
        },
        "expected_output": {"pick": pick},
    }


async def _run(root: Path, jev: ScriptedJev) -> list[tuple[str, bool]]:
    smoke_set = read_smoke_set(
        "exercise_shortlist", smoke_root=root, known_templates={EXERCISE_SHORTLIST_ID: 1}
    )
    results = await run_exercise_shortlist_smoke(smoke_set, model=jev, threshold=_THRESHOLD)
    return [(result.name, result.passed) for result in results]


async def test_a_confident_pick_of_the_labelled_exercise_passes(tmp_path: Path) -> None:
    root = _set(tmp_path, _case("pull ups", "Pull-up", "Pull-up", "Chin-up"))

    assert await _run(root, ScriptedJev({"pull ups": ("Pull-up", 0.97)})) == [("pull ups", True)]


async def test_a_pick_of_the_wrong_exercise_fails(tmp_path: Path) -> None:
    root = _set(tmp_path, _case("pull ups", "Pull-up", "Pull-up", "Chin-up"))

    assert await _run(root, ScriptedJev({"pull ups": ("Chin-up", 0.97)})) == [("pull ups", False)]


async def test_a_pick_below_the_threshold_is_the_card_so_it_passes_a_none_label(
    tmp_path: Path,
) -> None:
    root = _set(
        tmp_path, _case("bench press", "none", "Barbell Bench Press", "Dumbbell Bench Press")
    )

    jev = ScriptedJev({"bench press": ("Barbell Bench Press", 0.6)})

    assert await _run(root, jev) == [("bench press", True)]


async def test_a_pick_below_the_threshold_fails_a_label_that_wanted_the_pick(
    tmp_path: Path,
) -> None:
    root = _set(tmp_path, _case("pull ups", "Pull-up", "Pull-up", "Chin-up"))

    assert await _run(root, ScriptedJev({"pull ups": ("Pull-up", 0.6)})) == [("pull ups", False)]


async def test_none_passes_a_none_label_and_fails_a_pick_label(tmp_path: Path) -> None:
    root = _set(
        tmp_path,
        _case("rising arms", "none", "Dumbbell Lateral Raise", "Dumbbell Front Raise"),
        _case("pull ups", "Pull-up", "Pull-up", "Chin-up"),
    )
    jev = ScriptedJev({"rising arms": ("none", 0.95), "pull ups": ("none", 0.95)})

    assert await _run(root, jev) == [("rising arms", True), ("pull ups", False)]


async def test_a_confident_pick_fails_a_none_label(tmp_path: Path) -> None:
    root = _set(
        tmp_path, _case("rising arms", "none", "Dumbbell Lateral Raise", "Dumbbell Front Raise")
    )

    jev = ScriptedJev({"rising arms": ("Dumbbell Lateral Raise", 0.99)})

    assert await _run(root, jev) == [("rising arms", False)]


async def test_a_failing_call_fails_its_case_and_the_rest_still_run(tmp_path: Path) -> None:
    root = _set(tmp_path, _case("pull ups", "Pull-up", "Pull-up", "Chin-up"))

    results = await run_exercise_shortlist_smoke(
        read_smoke_set(
            "exercise_shortlist", smoke_root=root, known_templates={EXERCISE_SHORTLIST_ID: 1}
        ),
        model=ScriptedJev({}, error=RuntimeError("jev is down")),
        threshold=_THRESHOLD,
    )

    [result] = results
    assert not result.passed
    assert "jev is down" in result.detail


async def test_a_label_naming_no_candidate_is_refused_before_any_call(tmp_path: Path) -> None:
    root = _set(tmp_path, _case("pull ups", "Muscle-up", "Pull-up", "Chin-up"))
    jev = ScriptedJev({"pull ups": ("Pull-up", 0.97)})

    with pytest.raises(SmokeError, match="Muscle-up"):
        await _run(root, jev)

    assert jev.states == []


async def test_a_case_listing_a_candidate_name_twice_is_refused(tmp_path: Path) -> None:
    """Candidates are told apart by name here, so a repeat would collapse into one option."""
    root = _set(tmp_path, _case("pull ups", "none", "Pull-up", "Pull-up"))

    with pytest.raises(SmokeError, match="twice"):
        await _run(root, ScriptedJev({}))


async def test_a_case_with_no_candidates_is_refused(tmp_path: Path) -> None:
    root = _set(tmp_path, _case("pull ups", "none"))

    with pytest.raises(SmokeError, match="candidate"):
        await _run(root, ScriptedJev({}))


async def test_the_cli_runs_a_decision_set_on_the_decision_model_at_the_settings_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _set(
        tmp_path,
        _case("pull ups", "Pull-up", "Pull-up", "Chin-up"),
        _case("bench press", "none", "Barbell Bench Press", "Dumbbell Bench Press"),
    )
    jev = ScriptedJev({"pull ups": ("Pull-up", 0.95), "bench press": ("Barbell Bench Press", 0.5)})
    built: list[str] = []

    def model_for(template_id: str) -> tuple[DecisionModel[None], str]:
        built.append(template_id)
        return jev, _MODEL_ID

    async def runner(smoke_set: SmokeSet, model: Model) -> list[SmokeCaseResult]:
        return await run_exercise_shortlist_smoke(smoke_set, model=model, threshold=_THRESHOLD)

    exit_code = await execute_smoke(
        _build_parser().parse_args(["smoke", "exercise_shortlist"]),
        smoke_root=root,
        registry=PromptRegistry(root=ROOT),
        versions={EXERCISE_SHORTLIST_ID: 1},
        model_for=model_for,
        judge=lambda: (_ for _ in ()).throw(AssertionError("no judge")),
        decision_runners={EXERCISE_SHORTLIST_ID: runner},
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert built == [EXERCISE_SHORTLIST_ID]
    assert f"{EXERCISE_SHORTLIST_ID} v1 ({_MODEL_ID})" in out
    assert "PASS pull ups" in out
    assert "PASS bench press" in out


async def test_a_case_whose_candidates_are_malformed_is_refused(tmp_path: Path) -> None:
    root = _set(
        tmp_path,
        {
            "name": "pull ups",
            "inputs": {"prompt": "pull ups", "deps": {"candidates": "Pull-up"}},
            "expected_output": {"pick": "none"},
        },
    )

    with pytest.raises(SmokeError, match="candidates"):
        await _run(root, ScriptedJev({}))


def _env(monkeypatch: pytest.MonkeyPatch, *, key: str) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge/model")


def test_main_with_a_blank_key_runs_no_decision_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, key="  ")
    monkeypatch.setattr(cli_module, "_EVALS_ROOT", tmp_path)
    _set(tmp_path / "smoke", _case("pull ups", "Pull-up", "Pull-up", "Chin-up"))

    assert main(["smoke", "exercise_shortlist"]) == 1

    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY is blank" in err
    assert "nothing was run" in err


def test_main_builds_the_real_decision_model_and_never_reaches_a_real_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The wiring ran (settings, key, `TypeSafeModel`); the no-model-requests guard stops the
    call, which fails its case rather than reaching OpenRouter (ADR-0013)."""
    _env(monkeypatch, key="test-key")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    monkeypatch.setattr(cli_module, "_EVALS_ROOT", tmp_path)
    _set(tmp_path / "smoke", _case("pull ups", "Pull-up", "Pull-up", "Chin-up"))

    assert main(["smoke", "exercise_shortlist", "--model", "typesafe/jev-1.13"]) == 1

    out = capsys.readouterr().out
    assert "exercise_shortlist v1 (typesafe/jev-1.13)" in out
    assert "FAIL pull ups: the call failed" in out


# --- the committed set -------------------------------------------------------------------------


def test_the_committed_set_has_ten_cases_with_an_easy_a_tie_and_a_none() -> None:
    smoke_set = read_smoke_set(
        "exercise_shortlist", smoke_root=SMOKE_ROOT, known_templates={EXERCISE_SHORTLIST_ID: 1}
    )
    cases = yaml.safe_load((SMOKE_ROOT / "exercise_shortlist.yaml").read_text())["cases"]
    picks = [case for case in cases if not case["inputs"]["deps"].get("live")]
    labels = [case["expected_output"]["pick"] for case in picks]

    assert len(picks) == 10
    assert len(smoke_set.cases) == len(cases)
    assert "none" in labels
    assert any(label != "none" for label in labels)
    # The pick cases all pass; only live recall cases are known gaps, each with its ticket (#508).
    live = {case["name"] for case in cases if case["inputs"]["deps"].get("live")}
    assert set(smoke_set.known_gaps) <= live
    assert set(smoke_set.known_gaps.values()) == {"#508"}
    assert not smoke_set.has_judge


def test_the_wger_cases_pick_one_not_the_top_and_abstain_on_a_tie_and_a_miss() -> None:
    """The wger import's names (#555): each case is a wger English name and the catalogue-only
    shortlist recorded for it, best first; one pick is the trigram top, one is not, and two are
    "none" (a near tie, and a shortlist with no right exercise)."""
    cases = yaml.safe_load((SMOKE_ROOT / "exercise_shortlist.yaml").read_text())["cases"]
    wger = [case for case in cases if str(case["metadata"]["source"]).startswith("wger exercise")]
    tops = [case["inputs"]["deps"]["candidates"][0]["name"] for case in wger]
    labels = [case["expected_output"]["pick"] for case in wger]

    assert len(wger) >= 4
    assert any(label == top for label, top in zip(labels, tops, strict=True))
    assert any(label not in {"none", top} for label, top in zip(labels, tops, strict=True))
    assert labels.count("none") >= 2


def test_every_committed_label_names_one_of_its_candidates() -> None:
    cases = yaml.safe_load((SMOKE_ROOT / "exercise_shortlist.yaml").read_text())["cases"]

    for case in cases:
        if case["inputs"]["deps"].get("live"):
            continue
        names = [c["name"] for c in case["inputs"]["deps"]["candidates"]]
        assert case["expected_output"]["pick"] in {"none", *names}, case["name"]
        assert 1 <= len(names) <= Settings().exercise_shortlist_size, case["name"]


def test_the_default_threshold_starts_conservative() -> None:
    assert Settings().exercise_pick_threshold == 0.9


# --- live recall cases (ticket #429) -----------------------------------------------------------


def _live(
    name: str, listed: str | None = "Dumbbell Lateral Raise", measure: str | None = None
) -> dict[str, object]:
    expected: dict[str, object] = {} if listed is None else {"listed": listed}
    deps: dict[str, object] = {"live": True} | ({} if measure is None else {"measure": measure})
    return {"name": name, "inputs": {"prompt": name, "deps": deps}, "expected_output": expected}


class _Shortlist:
    """The app's shortlist, scripted by typed name. Records every ask."""

    def __init__(self, found: dict[str, list[str]], *, error: Exception | None = None) -> None:
        self.found = found
        self.error = error
        self.asked: list[str] = []
        self.measures: list[str] = []

    async def __call__(self, name: str, measure: str) -> list[ExerciseCandidate]:
        self.asked.append(name)
        self.measures.append(measure)
        if self.error is not None:
            raise self.error
        return [ExerciseCandidate(id=uuid4(), name=n) for n in self.found.get(name, [])]


async def _run_live(
    root: Path, shortlist: _Shortlist | None, jev: ScriptedJev | None = None
) -> list[tuple[str, bool, str]]:
    smoke_set = read_smoke_set(
        "exercise_shortlist", smoke_root=root, known_templates={EXERCISE_SHORTLIST_ID: 1}
    )
    results = await run_exercise_shortlist_smoke(
        smoke_set, model=jev or ScriptedJev({}), threshold=_THRESHOLD, shortlist=shortlist
    )
    return [(r.name, r.passed, r.detail) for r in results]


async def test_a_live_case_passes_when_the_shortlist_lists_the_exercise(tmp_path: Path) -> None:
    root = _set(tmp_path, _live("rising straight arms"))
    shortlist = _Shortlist(
        {"rising straight arms": ["Stability Ball Crunch", "Dumbbell Lateral Raise"]}
    )

    jev = ScriptedJev({})

    assert await _run_live(root, shortlist, jev) == [("rising straight arms", True, "")]
    assert shortlist.asked == ["rising straight arms"]
    assert jev.states == []  # a live case makes no Jev call


async def test_a_live_case_fails_naming_what_the_shortlist_listed(tmp_path: Path) -> None:
    root = _set(tmp_path, _live("rising straight arms"))
    shortlist = _Shortlist({"rising straight arms": ["Stability Ball Crunch"]})

    [(_, passed, detail)] = await _run_live(root, shortlist)

    assert not passed
    assert "Dumbbell Lateral Raise" in detail
    assert "Stability Ball Crunch" in detail


async def test_a_failing_shortlist_fails_its_live_case_and_the_rest_still_run(
    tmp_path: Path,
) -> None:
    root = _set(tmp_path, _live("one"), _case("pull ups", "none", "Pull-up", "Chin-up"))

    results = await _run_live(root, _Shortlist({}, error=RuntimeError("db is down")))

    live, pick = results
    assert live[:2] == ("one", False)
    assert "db is down" in live[2]
    assert pick[0] == "pull ups"


async def test_a_live_case_without_a_database_fails_rather_than_passing(tmp_path: Path) -> None:
    [(_, passed, detail)] = await _run_live(_set(tmp_path, _live("one")), None)

    assert not passed
    assert "database" in detail


async def test_a_live_case_with_no_exercise_to_list_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SmokeError, match="listed"):
        await _run_live(_set(tmp_path, _live("one", listed=None)), _Shortlist({}))


async def test_a_live_case_that_also_lists_candidates_is_refused(tmp_path: Path) -> None:
    case = _live("one")
    case["inputs"] = {"prompt": "one", "deps": {"live": True, "candidates": [{"name": "A"}]}}

    with pytest.raises(SmokeError, match="lists `candidates`"):
        await _run_live(_set(tmp_path, case), _Shortlist({}))


async def test_a_live_case_asks_for_the_measure_the_entry_has_and_defaults_to_load_reps(
    tmp_path: Path,
) -> None:
    root = _set(tmp_path, _live("one"), _live("two", measure="reps"))
    shortlist = _Shortlist({})

    await _run_live(root, shortlist)

    assert shortlist.measures == ["load_reps", "reps"]


async def test_a_live_case_with_an_unknown_measure_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SmokeError, match="measure"):
        await _run_live(_set(tmp_path, _live("one", measure="furlongs")), _Shortlist({}))


async def test_a_live_case_with_a_pick_is_refused_and_says_why(tmp_path: Path) -> None:
    case = _live("one")
    case["expected_output"] = {"listed": "Face Pull", "pick": "none"}

    with pytest.raises(SmokeError, match="Jev isn't asked"):
        await _run_live(_set(tmp_path, case), _Shortlist({}))


class _Opens:
    """The entry point's way of opening the live shortlist, counting how often it is opened."""

    def __init__(self, shortlist: _Shortlist) -> None:
        self.shortlist = shortlist
        self.opened = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[_Shortlist]:
        self.opened += 1
        yield self.shortlist


def _runner(opens: _Opens | None) -> DecisionSmokeRunner:
    decision_runners, _ = cli_module._own_runners(
        Settings(), PromptRegistry(root=ROOT), {EXERCISE_SHORTLIST_ID: 1}, opens
    )
    return decision_runners[EXERCISE_SHORTLIST_ID]


def _smoke_set(root: Path) -> SmokeSet:
    return read_smoke_set(
        "exercise_shortlist", smoke_root=root, known_templates={EXERCISE_SHORTLIST_ID: 1}
    )


async def test_the_cli_opens_the_live_shortlist_once_for_a_set_with_live_cases(
    tmp_path: Path,
) -> None:
    opens = _Opens(_Shortlist({"one": ["Dumbbell Lateral Raise"]}))
    smoke_set = _smoke_set(_set(tmp_path, _live("one")))

    results = await _runner(opens)(smoke_set, ScriptedJev({}))

    assert [(r.name, r.passed) for r in results] == [("one", True)]
    assert opens.opened == 1


async def test_the_cli_opens_no_database_for_a_set_of_picks_only(tmp_path: Path) -> None:
    opens = _Opens(_Shortlist({}))
    smoke_set = _smoke_set(_set(tmp_path, _case("pull ups", "none", "Pull-up", "Chin-up")))

    await _runner(opens)(smoke_set, ScriptedJev({"pull ups": ("Pull-up", 0.5)}))

    assert opens.opened == 0


async def test_the_cli_without_the_entry_points_database_fails_a_live_case_by_name(
    tmp_path: Path,
) -> None:
    [result] = await _runner(None)(_smoke_set(_set(tmp_path, _live("one"))), ScriptedJev({}))

    assert not result.passed
    assert "database" in result.detail


def test_main_hands_the_live_shortlister_to_the_smoke_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, key="test-key")
    handed: list[object] = []

    def smoke(args: object, settings: object, live: object) -> int:
        handed.append(live)
        return 0

    monkeypatch.setattr(cli_module, "_main_smoke", smoke)
    opens = _Opens(_Shortlist({}))

    assert main(["smoke", "exercise_shortlist"], live_shortlister=opens) == 0

    assert handed == [opens]


def test_the_entry_point_builds_the_live_shortlister_from_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, key="test-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    seen: dict[str, object] = {}

    def run(argv: list[str] | None, *, live_shortlister: LiveShortlister) -> int:
        seen["argv"] = argv
        seen["live"] = live_shortlister()
        return 7

    monkeypatch.setattr(evals_main, "run_evals", run)

    assert evals_main.main(["smoke", "exercise_shortlist"]) == 7
    assert seen["argv"] == ["smoke", "exercise_shortlist"]
    assert seen["live"] is not None  # an unopened context manager: no database was touched
