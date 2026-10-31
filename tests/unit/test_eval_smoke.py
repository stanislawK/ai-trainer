"""Smoke sets (ADR-0034, ticket #372): `evals/smoke/<id>.yaml` run once per case, graded in code,
no baseline, no judge except the safety set. No real model: scripted `FunctionModel`s."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals.cli import _build_parser, execute_smoke, main
from ai_trainer.llm.evals.inputs import EvalCaseInputs
from ai_trainer.llm.evals.smoke import SmokeError, read_smoke_set, smoke_ids
from ai_trainer.llm.prompts.registry import PromptRegistry
from ai_trainer.llm.prompts.template import PromptTemplate

ROOT = Path(__file__).resolve().parents[2]
_FIXTURES_ROOT = ROOT / "tests" / "fixtures" / "prompts"
_MODEL_ID = "under-test/model"
_JUDGE_ID = "judge/model"


class _Deps(BaseModel):
    name: str
    sport: str


class _Output(BaseModel):
    reply: str


def _template(template_id: str) -> PromptTemplate[_Deps, _Output]:
    return PromptTemplate(
        id=template_id,
        version=1,
        locale="en",
        deps_type=_Deps,
        output_type=_Output,
        model_settings_key="sample_template_model",
    )


def _registry(*template_ids: str) -> PromptRegistry:
    registry = PromptRegistry(root=_FIXTURES_ROOT)
    for template_id in template_ids:
        registry.register(_template(template_id))
    return registry


def _case(name: str, expected_reply: str) -> dict[str, object]:
    return {
        "name": name,
        "inputs": {"prompt": "hi", "deps": {"name": "Alex", "sport": "climbing"}},
        "expected_output": {"reply": expected_reply},
    }


def _write_set(
    root: Path,
    smoke_id: str,
    cases: list[dict[str, object]],
    *,
    template: str = "sample",
    known_gaps: dict[str, str] | None = None,
    evaluators: list[object] | None = None,
) -> None:
    body: dict[str, object] = {
        "name": smoke_id,
        "template": template,
        "cases": cases,
        "evaluators": evaluators if evaluators is not None else ["EqualsExpected"],
    }
    if known_gaps is not None:
        body["known_gaps"] = known_gaps
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{smoke_id}.yaml").write_text(yaml.safe_dump(body, sort_keys=False))


class _Calls:
    """What the run asked for, so a test can assert no model was built."""

    def __init__(self, reply: str = "hi Alex") -> None:
        self.reply = reply
        self.models: list[str] = []
        self.judges = 0
        self.model_requests = 0

    def model_for(self, template_id: str) -> tuple[Model, str]:
        self.models.append(template_id)

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.model_requests += 1
            return ModelResponse(parts=[TextPart(f'{{"reply": "{self.reply}"}}')])

        return FunctionModel(respond), _MODEL_ID

    def judge(self) -> tuple[Model, str]:
        self.judges += 1

        def passes(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name, {"reason": "fine", "pass": True, "score": 1.0}
                    )
                ]
            )

        return FunctionModel(passes), _JUDGE_ID


async def _run(root: Path, argv: list[str], calls: _Calls, *, registry: PromptRegistry) -> int:
    return await execute_smoke(
        _build_parser().parse_args(["smoke", *argv]),
        smoke_root=root,
        registry=registry,
        versions={"sample": 1, "chitchat": 1},
        model_for=calls.model_for,
        judge=calls.judge,
    )


async def test_smoke_prints_each_case_and_exits_zero_when_all_pass_or_known_gap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_set(
        tmp_path,
        "sample",
        [_case("greets", "hi Alex"), _case("wrong reply", "bye")],
        known_gaps={"wrong reply": "#373"},
    )

    exit_code = await _run(tmp_path, ["sample"], _Calls(), registry=_registry("sample"))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "PASS greets" in out
    assert "KNOWN GAP #373 wrong reply" in out


async def test_smoke_exits_nonzero_on_a_failing_case_not_under_known_gaps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_set(tmp_path, "sample", [_case("greets", "hi Alex"), _case("wrong reply", "bye")])

    exit_code = await _run(tmp_path, ["sample"], _Calls(), registry=_registry("sample"))

    assert exit_code == 1
    assert "FAIL wrong reply" in capsys.readouterr().out


async def test_smoke_notes_a_known_gap_that_now_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_set(tmp_path, "sample", [_case("greets", "hi Alex")], known_gaps={"greets": "#373"})

    exit_code = await _run(tmp_path, ["sample"], _Calls(), registry=_registry("sample"))

    assert exit_code == 0
    assert "FIXED? #373 greets" in capsys.readouterr().out


async def test_smoke_runs_each_case_once_and_writes_no_baseline(tmp_path: Path) -> None:
    _write_set(tmp_path / "smoke", "sample", [_case("a", "hi Alex"), _case("b", "hi Alex")])
    calls = _Calls()

    await _run(tmp_path / "smoke", ["sample"], calls, registry=_registry("sample"))

    assert calls.model_requests == 2
    assert [path.name for path in tmp_path.iterdir()] == ["smoke"]


async def test_all_runs_every_set_in_the_smoke_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_set(tmp_path, "sample", [_case("one", "hi Alex")])
    _write_set(tmp_path, "other", [_case("two", "hi Alex")], template="sample")
    calls = _Calls()

    exit_code = await _run(tmp_path, ["--all"], calls, registry=_registry("sample"))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "PASS one" in out
    assert "PASS two" in out
    assert smoke_ids(tmp_path) == ["other", "sample"]


async def test_all_with_no_sets_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _Calls()

    exit_code = await _run(tmp_path, ["--all"], calls, registry=_registry("sample"))

    assert exit_code == 1
    assert "No smoke sets" in capsys.readouterr().err
    assert calls.models == []


async def test_unknown_id_errors_clearly_and_builds_no_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_set(tmp_path, "sample", [_case("greets", "hi Alex")])
    calls = _Calls()

    exit_code = await _run(tmp_path, ["nope"], calls, registry=_registry("sample"))

    err = capsys.readouterr().err
    assert exit_code == 1
    assert "No smoke set named 'nope'" in err
    assert "sample" in err
    assert calls.models == []
    assert calls.judges == 0
    assert calls.model_requests == 0


async def test_one_bad_set_stops_the_run_before_any_model_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_set(tmp_path, "good", [_case("greets", "hi Alex")])
    _write_set(tmp_path, "bad", [_case("greets", "hi Alex")], known_gaps={"missing": "#373"})
    calls = _Calls()

    exit_code = await _run(tmp_path, ["--all"], calls, registry=_registry("sample"))

    assert exit_code == 1
    assert "'missing'" in capsys.readouterr().err
    assert calls.models == []


def test_a_known_gap_needs_a_ticket_number(tmp_path: Path) -> None:
    _write_set(tmp_path, "sample", [_case("greets", "hi")], known_gaps={"greets": "later"})

    with pytest.raises(SmokeError, match="ticket"):
        read_smoke_set("sample", smoke_root=tmp_path)


def test_a_set_for_an_unknown_template_is_refused(tmp_path: Path) -> None:
    _write_set(tmp_path, "sample", [_case("greets", "hi")], template="ghost")

    with pytest.raises(SmokeError, match="ghost"):
        read_smoke_set("sample", smoke_root=tmp_path, known_templates={"sample"})


_JUDGE = {
    "LLMJudge": {
        "rubric": "Safe",
        "include_input": True,
        "assertion": {"evaluation_name": "pain_safety"},
    }
}


async def test_the_safety_set_runs_with_its_judge(tmp_path: Path) -> None:
    _write_set(
        tmp_path,
        "safety",
        [_case("pain", "hi Alex")],
        template="sample",
        evaluators=[_JUDGE],
    )
    calls = _Calls()

    exit_code = await _run(tmp_path, ["safety"], calls, registry=_registry("sample"))

    assert exit_code == 0
    assert calls.judges == 1


async def test_a_set_other_than_safety_makes_no_judge_call_and_may_not_hold_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_set(tmp_path, "sample", [_case("greets", "hi Alex")], evaluators=[_JUDGE])
    calls = _Calls()

    exit_code = await _run(tmp_path, ["sample"], calls, registry=_registry("sample"))

    assert exit_code == 1
    assert "only the safety set" in capsys.readouterr().err
    assert calls.judges == 0
    assert calls.models == []


async def test_a_code_graded_set_never_builds_the_judge(tmp_path: Path) -> None:
    _write_set(tmp_path, "sample", [_case("greets", "hi Alex")])
    calls = _Calls()

    await _run(tmp_path, ["sample"], calls, registry=_registry("sample"))

    assert calls.judges == 0


def test_the_parser_takes_one_id_or_all() -> None:
    parser = _build_parser()

    assert parser.parse_args(["smoke", "gym.extract"]).smoke_id == "gym.extract"
    assert parser.parse_args(["smoke", "--all"]).all is True
    with pytest.raises(SystemExit):
        main(["smoke"])
    with pytest.raises(SystemExit):
        main(["smoke", "gym.extract", "--all"])


def test_gym_smoke_cases_cite_a_transcript_verbatim_or_the_ticket_that_reported_them() -> None:
    # ADR-0034: mostly cut from field transcripts, synthetic only for a reported bug.
    smoke_set = read_smoke_set("gym.extract", smoke_root=ROOT / "evals" / "smoke")

    assert smoke_set.cases
    cited = 0
    for case in smoke_set.cases:
        assert case.metadata is not None, case.name
        if "reported" in case.metadata:
            assert re.fullmatch(r"#\d+", str(case.metadata["reported"])), case.name
            continue
        transcript = ROOT / str(case.metadata["field"])
        assert transcript.is_file(), case.metadata["field"]
        assert case.inputs.prompt in transcript.read_text(), case.name
        cited += 1
    assert cited > len(smoke_set.cases) - cited


def _gym_smoke_case(prompt: str) -> dict[str, Any]:
    dataset = read_smoke_set("gym.extract", smoke_root=ROOT / "evals" / "smoke").dataset
    cases: list[dict[str, Any]] = dataset["cases"]  # type: ignore[assignment]  # read as YAML
    return next(case for case in cases if case["inputs"]["prompt"] == prompt)


_HANTLES = "4 series two hantles 6kg each - rising straight arms on both sides"
_BENCH = "Bench press 45kg 8 x3"


def test_gym_smoke_expects_four_sets_and_empty_reps_for_the_hantles_message() -> None:
    activity = _gym_smoke_case(_HANTLES)["expected_output"]["activities"][0]

    assert (activity["sets"], activity["reps"]) == (4, None)


def test_gym_smoke_grades_the_hantles_load_at_6_kg() -> None:
    case = _gym_smoke_case(_HANTLES)

    assert "load" not in case["metadata"]["ignore_fields"]
    assert case["expected_output"]["activities"][0]["load"] == {
        "original": "6kg",
        "value": 6,
        "unit": "kg",
    }


def test_gym_smoke_expects_bench_at_3_sets_of_8_at_45_kg() -> None:
    activity = _gym_smoke_case(_BENCH)["expected_output"]["activities"][0]

    assert (activity["sets"], activity["reps"], activity["load"]["value"]) == (3, 8, 45)


def test_gym_smoke_expects_face_pull_at_4_sets_of_12_at_30_kg() -> None:
    case = _gym_smoke_case("Face pull 30kg 12reps 4 series")
    activity = case["expected_output"]["activities"][0]

    assert (activity["sets"], activity["reps"], activity["load"]["value"]) == (4, 12, 30)


def test_gym_smoke_lists_neither_the_bench_nor_the_hantles_case_as_a_known_gap() -> None:
    known_gaps = read_smoke_set("gym.extract", smoke_root=ROOT / "evals" / "smoke").known_gaps

    for prompt in (_BENCH, _HANTLES):
        assert _gym_smoke_case(prompt)["name"] not in known_gaps


def test_the_committed_smoke_sets_read_and_the_safety_set_alone_holds_a_judge() -> None:
    root = ROOT / "evals" / "smoke"

    for smoke_id in smoke_ids(root):
        smoke_set = read_smoke_set(smoke_id, smoke_root=root)
        assert smoke_set.has_judge == (smoke_id == "safety"), smoke_id


@pytest.mark.parametrize("path", ["smoke/safety.yaml", "datasets/chitchat.yaml"])
def test_every_pain_rubric_allows_stopping_but_bans_alternatives(path: str) -> None:
    raw = yaml.safe_load((ROOT / "evals" / path).read_text())
    judges = [
        (case["name"], evaluator["LLMJudge"])
        for case in raw["cases"]
        for evaluator in case.get("evaluators") or []
        if "LLMJudge" in evaluator
    ]
    pain = [(name, judge) for name, judge in judges if name.startswith("pain:")]

    assert pain
    for name, judge in pain:
        rubric = " ".join(judge["rubric"].split())
        assert "stop what hurts until they are seen is fine" in rubric, name
        assert "suggesting alternatives or what they can still do is not" in rubric, name


_TEMPLATE_SMOKE_IDS = (
    "router",
    "climbing.extract",
    "cycling.extract",
    "onboarding.about_you",
    "onboarding.goals",
    "onboarding.level",
    "onboarding.schedule",
)


def test_every_template_smoke_set_the_ticket_lists_is_committed() -> None:
    root = ROOT / "evals" / "smoke"
    versions = cli_module._active_versions(default_sport_registry())

    assert set(_TEMPLATE_SMOKE_IDS) <= set(smoke_ids(root))
    for smoke_id in _TEMPLATE_SMOKE_IDS:
        read_smoke_set(smoke_id, smoke_root=root, known_templates=versions)


def test_every_template_smoke_set_has_at_least_five_cases() -> None:
    root = ROOT / "evals" / "smoke"

    # `safety` is the judged set the owner keeps apart (it has 4 cases); not part of #374.
    for smoke_id in (*_TEMPLATE_SMOKE_IDS, "gym.extract"):
        assert len(read_smoke_set(smoke_id, smoke_root=root).cases) >= 5, smoke_id


def test_every_new_smoke_case_names_its_dataset_origin() -> None:
    root = ROOT / "evals" / "smoke"

    for smoke_id in _TEMPLATE_SMOKE_IDS:
        for case in read_smoke_set(smoke_id, smoke_root=root).cases:
            assert case.metadata is not None, (smoke_id, case.name)
            origin = ROOT / str(case.metadata["dataset"])
            assert origin.is_file(), (smoke_id, case.metadata["dataset"])
            matches = [
                c for c in yaml.safe_load(origin.read_text())["cases"] if c["name"] == case.name
            ]
            assert len(matches) == 1, (smoke_id, case.name)
            assert case.inputs.model_dump() == matches[0]["inputs"], (smoke_id, case.name)


def test_no_workflow_runs_a_smoke_set() -> None:
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        assert "evals smoke" not in workflow.read_text(), workflow.name


def test_a_set_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    (tmp_path / "sample.yaml").write_text("- just\n- a list\n")

    with pytest.raises(SmokeError, match="mapping"):
        read_smoke_set("sample", smoke_root=tmp_path)


def test_the_safety_set_takes_no_known_gaps(tmp_path: Path) -> None:
    _write_set(
        tmp_path,
        "safety",
        [_case("pain", "hi Alex")],
        known_gaps={"pain": "#382"},
        evaluators=[_JUDGE],
    )

    with pytest.raises(SmokeError, match="must pass"):
        read_smoke_set("safety", smoke_root=tmp_path)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("name: s\n", "at least one case"),
        ("name: s\ncases: []\n", "at least one case"),
        ("name: s\ncases: nope\n", "at least one case"),
        ("name: s\ncases:\n- just a string\n", "s.yaml"),
        (
            "name: s\nknown_gaps: [x]\ncases:\n" + "- {name: a, inputs: {prompt: p, deps: {}}}\n",
            "map",
        ),
        (
            "name: s\ncases:\n- {name: a, inputs: {prompt: p, deps: {}}}\n"
            "- {name: a, inputs: {prompt: q, deps: {}}}\n",
            "two cases are named 'a'",
        ),
    ],
    ids=[
        "no-cases-key",
        "empty-cases",
        "cases-not-a-list",
        "case-not-a-mapping",
        "gaps-list",
        "dup",
    ],
)
def test_a_malformed_set_is_a_smoke_error_not_a_traceback(
    tmp_path: Path, body: str, message: str
) -> None:
    (tmp_path / "s.yaml").write_text(body)

    with pytest.raises(SmokeError, match=message):
        read_smoke_set("s", smoke_root=tmp_path)


def test_an_id_that_climbs_out_of_the_smoke_folder_is_unknown(tmp_path: Path) -> None:
    _write_set(tmp_path, "inside", [_case("a", "hi")])
    (tmp_path.parent / "outside.yaml").write_text("name: outside\n")

    with pytest.raises(SmokeError, match="No smoke set named"):
        read_smoke_set("../outside", smoke_root=tmp_path)


def test_a_case_without_inputs_is_refused(tmp_path: Path) -> None:
    (tmp_path / "sample.yaml").write_text("name: sample\ncases:\n- name: lonely\n")

    with pytest.raises(SmokeError, match=r"sample\.yaml"):
        read_smoke_set("sample", smoke_root=tmp_path)


def test_the_safety_set_without_its_judge_is_refused(tmp_path: Path) -> None:
    _write_set(tmp_path, "safety", [_case("pain", "hi Alex")])

    with pytest.raises(SmokeError, match="always runs with its judge"):
        read_smoke_set("safety", smoke_root=tmp_path)


def test_a_judge_on_a_single_case_counts_as_a_judge(tmp_path: Path) -> None:
    _write_set(tmp_path, "sample", [{**_case("greets", "hi Alex"), "evaluators": [_JUDGE]}])

    with pytest.raises(SmokeError, match="only the safety set"):
        read_smoke_set("sample", smoke_root=tmp_path)


async def test_the_judge_may_not_be_the_model_under_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_set(tmp_path, "safety", [_case("pain", "hi Alex")], evaluators=[_JUDGE])
    calls = _Calls()

    def same_as_model() -> tuple[Model, str]:
        return calls.judge()[0], _MODEL_ID

    exit_code = await execute_smoke(
        _build_parser().parse_args(["smoke", "safety"]),
        smoke_root=tmp_path,
        registry=_registry("sample"),
        versions={"sample": 1},
        model_for=lambda template_id: calls.model_for("sample"),
        judge=same_as_model,
    )

    assert exit_code == 1
    assert "judge model must differ" in capsys.readouterr().err
    assert calls.model_requests == 0


async def test_a_judge_clash_is_found_before_an_earlier_set_spends_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_set(tmp_path, "aaa", [_case("greets", "hi Alex")])
    _write_set(tmp_path, "safety", [_case("pain", "hi Alex")], evaluators=[_JUDGE])
    calls = _Calls()

    exit_code = await execute_smoke(
        _build_parser().parse_args(["smoke", "--all"]),
        smoke_root=tmp_path,
        registry=_registry("sample"),
        versions={"sample": 1},
        model_for=calls.model_for,
        judge=lambda: (calls.judge()[0], _MODEL_ID),
    )

    assert exit_code == 1
    assert calls.model_requests == 0
    assert "judge model must differ" in capsys.readouterr().err


async def test_a_case_whose_model_call_fails_is_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_set(tmp_path, "sample", [_case("greets", "hi Alex")])

    def broken(template_id: str) -> tuple[Model, str]:
        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            raise RuntimeError("upstream down")

        return FunctionModel(respond), _MODEL_ID

    exit_code = await execute_smoke(
        _build_parser().parse_args(["smoke", "sample"]),
        smoke_root=tmp_path,
        registry=_registry("sample"),
        versions={"sample": 1},
        model_for=broken,
        judge=_Calls().judge,
    )

    assert exit_code == 1
    assert "FAIL greets: RuntimeError: upstream down" in capsys.readouterr().out


async def test_a_case_nothing_asserts_on_does_not_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_set(tmp_path, "sample", [_case("greets", "hi Alex")], evaluators=[])

    exit_code = await _run(tmp_path, ["sample"], _Calls(), registry=_registry("sample"))

    assert exit_code == 1
    assert "no evaluator asserted" in capsys.readouterr().out


@dataclass
class _CrashingEvaluator(Evaluator[EvalCaseInputs, BaseModel]):
    def evaluate(self, ctx: EvaluatorContext[EvalCaseInputs, BaseModel]) -> bool:
        raise RuntimeError("boom")


async def test_a_crashing_evaluator_fails_the_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(cli_module._EVALUATORS, "sample", (_CrashingEvaluator,))
    _write_set(tmp_path, "sample", [_case("greets", "hi Alex")], evaluators=["_CrashingEvaluator"])

    exit_code = await _run(tmp_path, ["sample"], _Calls(), registry=_registry("sample"))

    assert exit_code == 1
    assert (
        "FAIL greets: evaluator crashed: _CrashingEvaluator: RuntimeError: boom"
        in capsys.readouterr().out
    )


def _env(monkeypatch: pytest.MonkeyPatch, *, key: str) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge/model")


def test_main_with_an_unknown_id_errors_before_it_needs_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, key="")
    monkeypatch.setattr(cli_module, "_EVALS_ROOT", tmp_path)

    assert main(["smoke", "nope"]) == 1

    err = capsys.readouterr().err
    assert "No smoke set named 'nope'" in err
    assert "OPENROUTER_API_KEY" not in err


def test_main_with_a_blank_key_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _env(monkeypatch, key="  ")
    monkeypatch.setattr(cli_module, "_EVALS_ROOT", tmp_path)
    _write_set(
        tmp_path / "smoke", "gym.extract", [_case("greets", "hi Alex")], template="gym.extract"
    )

    assert main(["smoke", "gym.extract"]) == 1

    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY is blank" in err
    assert "nothing was run" in err


def test_main_builds_the_real_models_and_never_reaches_a_real_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The case's deps don't fit chitchat's, so it fails before any request; the wiring ran
    (settings, provider, model, judge) and `conftest.py` forbids a real call anyway."""
    _env(monkeypatch, key="test-key")
    monkeypatch.setattr(cli_module, "_EVALS_ROOT", tmp_path)
    _write_set(
        tmp_path / "smoke",
        "safety",
        [{"name": "pain", "inputs": {"prompt": "hi", "deps": {}}}],
        template="chitchat",
        evaluators=[_JUDGE],
    )

    assert main(["smoke", "safety", "--model", "under-test/model"]) == 1

    assert "FAIL pain" in capsys.readouterr().out
