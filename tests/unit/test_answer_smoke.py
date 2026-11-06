"""The `gym.answer` smoke runner (ADR-0034, #421): each case runs through the real MCP server and
hook over its own fixture, and the reply is graded in code. No real model here: a scripted one
reads the tools the way the template asks and says what a test needs."""

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic_ai import ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_trainer.domain.sports.registry import default_sport_registry
from ai_trainer.llm.conversation import build_prompt_registry
from ai_trainer.llm.evals import cli as cli_module
from ai_trainer.llm.evals.answer_smoke import _read_cases, run_answer_smoke
from ai_trainer.llm.evals.cli import main
from ai_trainer.llm.evals.smoke import SmokeError, read_smoke_set

_REPO = Path(__file__).parents[2]
_PROMPTS = _REPO / "src" / "ai_trainer" / "llm" / "prompts"
_REGISTRY = build_prompt_registry(_PROMPTS, default_sport_registry())
# The version the app serves, so the harness's tests follow `/tune-prompt` bumps.
_GYM_ANSWERS = default_sport_registry().get("gym").answers
assert _GYM_ANSWERS is not None
_VERSION = _GYM_ANSWERS.version
_DEPS = {
    "message": "How is my bench going?",
    "history": [],
    "today": "2026-11-02",
    "now_local": "2026-11-02T18:30:00+01:00",
    "timezone": "Europe/Warsaw",
}
_BENCH: dict[str, Any] = {
    "day": "2026-10-28",
    "typed": "Bench press",
    "exercise": "Barbell Bench Press",
    "sets": [{"reps": 8, "load_kg": 45, "load_original": "45kg"}],
}


def _returns(messages: list[ModelMessage]) -> list[str]:
    return [
        json.dumps(part.content, default=str)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def _reading(say: Callable[[list[str]], str], name: str = "bench") -> FunctionModel:
    """Calls `find_exercise`, then `exercise_history` on what it listed, then says what `say`
    makes of the returns."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = _returns(messages)
        if not returns:
            return ModelResponse(parts=[ToolCallPart("find_exercise", {"name": name})])
        if len(returns) == 1 and '"logged":[{' in returns[0].replace(" ", ""):
            found = json.loads(returns[0])
            row = found["data"]["logged"][0]
            args = (
                {"exercise_id": row["exercise_id"]}
                if row["exercise_id"]
                else {"typed_name": row["name"]}
            )
            return ModelResponse(parts=[ToolCallPart("exercise_history", args)])
        return ModelResponse(parts=[TextPart(say(returns))])

    return FunctionModel(respond)


def _set_with(tmp_path: Path, *cases: dict[str, Any]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "gym.answer.yaml").write_text(
        yaml.safe_dump({"name": "gym.answer", "cases": list(cases)}, sort_keys=False)
    )
    return tmp_path


def _case(metadata: dict[str, Any], name: str = "a case") -> dict[str, Any]:
    return {
        "name": name,
        "metadata": metadata,
        "inputs": {"prompt": "How is my bench going?", "deps": _DEPS},
    }


async def _run(root: Path, model: FunctionModel, max_requests: int = 6) -> list[Any]:
    smoke_set = read_smoke_set("gym.answer", smoke_root=root)
    return await run_answer_smoke(
        smoke_set, model, registry=_REGISTRY, version=_VERSION, max_requests=max_requests
    )


async def test_a_reply_from_the_tools_passes(tmp_path: Path) -> None:
    root = _set_with(
        tmp_path,
        _case(
            {
                "logged": [_BENCH],
                "expect": {
                    "called": ["find_exercise", "exercise_history"],
                    "mentions": [r"\b45\b"],
                },
            }
        ),
    )

    (result,) = await _run(root, _reading(lambda _: "3 sets at 45 kg on 28 Oct."))

    assert result.passed
    assert result.detail == ""


async def test_the_tools_read_the_cases_own_fixture(tmp_path: Path) -> None:
    seen: list[str] = []

    def remember(returns: list[str]) -> str:
        seen.extend(returns)
        return "Done."

    face_pull = {"day": "2026-10-28", "typed": "Face pull", "sets": [{"reps": 12}]}
    root = _set_with(tmp_path, _case({"logged": [_BENCH, face_pull], "expect": {}}))

    await _run(root, _reading(remember))

    assert "Barbell Bench Press" in seen[0]
    assert "Face pull" not in seen[0]
    assert '"reps":8' in seen[1].replace(" ", "")
    assert "2026-10-28" in seen[1]


async def test_a_typed_only_entry_is_read_by_its_name(tmp_path: Path) -> None:
    seen: list[str] = []

    def remember(returns: list[str]) -> str:
        seen.extend(returns)
        return "Done."

    typed = {
        "day": "2026-10-28",
        "typed": "Rising straight arms on both sides",
        "implements": 2,
        "sets": [{"load_kg": 6, "load_original": "6kg"}],
    }
    root = _set_with(tmp_path, _case({"logged": [typed], "expect": {}}))

    await _run(root, _reading(remember, name="rising straight arms"))

    assert '"exercise_id":null' in seen[0].replace(" ", "")
    assert '"implements":2' in seen[1].replace(" ", "")


async def test_a_catalogue_exercise_with_no_entries_is_not_history(tmp_path: Path) -> None:
    seen: list[str] = []

    def remember(returns: list[str]) -> str:
        seen.extend(returns)
        return "You haven't logged that yet."

    root = _set_with(
        tmp_path,
        _case(
            {
                "catalogue": ["Barbell Bench Press"],
                "expect": {"no_digits": True, "called": ["find_exercise"]},
            }
        ),
    )

    (result,) = await _run(root, _reading(remember))

    assert result.passed
    assert "Barbell Bench Press" in seen[0]
    assert '"logged":[]' in seen[0].replace(" ", "")


@pytest.mark.parametrize(
    ("expect", "reply", "miss"),
    [
        ({"mentions": [r"\b45\b"]}, "Going well.", "no mention of"),
        ({"forbids": [r"\btry\b"]}, "Try more.", "says"),
        ({"no_digits": True}, "Bench is 45 kg.", "gives a number"),
        ({"called": ["query_sessions"]}, "Fine.", "never called query_sessions"),
    ],
    ids=["mentions", "forbids", "no-digits", "called"],
)
async def test_each_way_a_reply_can_miss_fails_the_case(
    tmp_path: Path, expect: dict[str, Any], reply: str, miss: str
) -> None:
    root = _set_with(tmp_path, _case({"logged": [_BENCH], "expect": expect}))

    (result,) = await _run(root, _reading(lambda _: reply))

    assert not result.passed
    assert miss in result.detail
    assert reply in result.detail


async def test_a_number_said_before_the_tools_fails_the_case(tmp_path: Path) -> None:
    """The app shows every request's text, so the grader reads all of it, not the last."""
    root = _set_with(tmp_path, _case({"logged": [_BENCH], "expect": {"forbids": [r"\b80\b"]}}))

    def guesses_first(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not _returns(messages):
            return ModelResponse(
                parts=[
                    TextPart("Last time you benched 80 kg, let me check."),
                    ToolCallPart("find_exercise", {"name": "bench"}),
                ]
            )
        return ModelResponse(parts=[TextPart("You benched 45 kg on 28 Oct.")])

    (result,) = await _run(root, FunctionModel(guesses_first))

    assert not result.passed
    assert "80 kg" in result.detail
    assert "45 kg" in result.detail


async def test_a_set_for_a_template_no_sport_names_is_refused(tmp_path: Path) -> None:
    root = _set_with(tmp_path, _case({"logged": [_BENCH], "expect": {}}))
    smoke_set = read_smoke_set("gym.answer", smoke_root=root)

    with pytest.raises(SmokeError, match="no sport names"):
        await run_answer_smoke(
            replace(smoke_set, template_id="rowing.answer"),
            _reading(lambda _: "Fine."),
            registry=_REGISTRY,
            version=_VERSION,
            max_requests=6,
        )


async def test_a_failed_call_fails_its_case_alone(tmp_path: Path) -> None:
    root = _set_with(
        tmp_path,
        _case({"logged": [_BENCH], "expect": {}}, name="first"),
        _case({"logged": [_BENCH], "expect": {}}, name="second"),
    )
    calls: list[int] = []

    def fail_first(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(len(messages))
        if len(calls) == 1:
            raise RuntimeError("upstream is down")
        return ModelResponse(parts=[TextPart("Fine.")])

    first, second = await _run(root, FunctionModel(fail_first))

    assert "the call failed" in first.detail
    assert second.passed


async def test_the_request_cap_fails_a_case_that_keeps_calling(tmp_path: Path) -> None:
    root = _set_with(tmp_path, _case({"logged": [_BENCH], "expect": {}}))

    def loops(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("query_sessions", {})])

    (result,) = await _run(root, FunctionModel(loops), max_requests=2)

    assert "the call failed" in result.detail


async def test_query_sessions_lists_the_fixtures_days_within_its_filters(tmp_path: Path) -> None:
    later = {**_BENCH, "day": "2026-11-01"}
    root = _set_with(tmp_path, _case({"logged": [_BENCH, later], "expect": {}}))
    seen: list[str] = []
    calls = [
        {},
        {"since": "2026-11-01"},
        {"until": "2026-10-28"},
        {"sport": "climbing"},
    ]

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = _returns(messages)
        if len(returns) < len(calls):
            return ModelResponse(parts=[ToolCallPart("query_sessions", calls[len(returns)])])
        seen.extend(returns)
        return ModelResponse(parts=[TextPart("Fine.")])

    await _run(root, FunctionModel(respond))

    assert "2026-11-01" in seen[0]
    assert "2026-10-28" in seen[0]
    assert "2026-10-28" not in seen[1]
    assert "2026-11-01" not in seen[2]
    assert '"success":false' in seen[3].replace(" ", "")


async def test_an_id_the_fixture_does_not_know_reads_as_not_found(tmp_path: Path) -> None:
    root = _set_with(tmp_path, _case({"logged": [_BENCH], "expect": {}}))
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = _returns(messages)
        if not returns:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "exercise_history",
                        {"exercise_id": "00000000-0000-0000-0000-000000000000"},
                    )
                ]
            )
        seen.extend(returns)
        return ModelResponse(parts=[TextPart("Fine.")])

    await _run(root, FunctionModel(respond))

    assert '"success":false' in seen[0].replace(" ", "")


def test_a_bad_fixture_is_refused_before_any_call(tmp_path: Path) -> None:
    root = _set_with(tmp_path, _case({"logged": [{"day": "not a day"}], "expect": {}}))
    smoke_set = read_smoke_set("gym.answer", smoke_root=root)

    with pytest.raises(SmokeError):
        _read_cases(smoke_set)


def test_a_bad_pattern_is_refused_before_any_call(tmp_path: Path) -> None:
    root = _set_with(tmp_path, _case({"expect": {"mentions": ["("]}}))
    smoke_set = read_smoke_set("gym.answer", smoke_root=root)

    with pytest.raises(SmokeError):
        _read_cases(smoke_set)


def test_main_runs_the_answer_set_on_the_tools_and_never_reaches_a_real_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The wiring ran (settings, key, the model under test, the answer runner); the
    no-model-requests guard fails the case rather than reaching OpenRouter (ADR-0013)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge/model")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    monkeypatch.setattr(cli_module, "_EVALS_ROOT", tmp_path)
    _set_with(tmp_path / "smoke", _case({"logged": [_BENCH], "expect": {}}))

    assert main(["smoke", "gym.answer"]) == 1

    out = capsys.readouterr().out
    assert f"gym.answer v{_VERSION} (z-ai/glm-5.3-flash)" in out
    assert "FAIL a case: the call failed" in out


def test_the_committed_set_reads_and_every_case_names_its_source() -> None:
    smoke_set = read_smoke_set("gym.answer", smoke_root=_REPO / "evals" / "smoke")

    cases = _read_cases(smoke_set)

    assert 5 <= len(cases) <= 10
    raw = yaml.safe_load((_REPO / "evals" / "smoke" / "gym.answer.yaml").read_text())
    for case in raw["cases"]:
        assert {"field", "prd", "safety"} & set(case["metadata"]), case["name"]


# Cited answers (#428): a case may carry knowledge passages, and `cites` grades which sources
# the reply showed, read from the run as the app reads it (`mcp/citations.py`).

_DELOAD_PASSAGE: dict[str, Any] = {
    "title": "Periodization",
    "author": "Tudor Bompa",
    "evidence_tier": "coaching_book",
    "source_type": "book",
    "locator": {"kind": "page", "page": 12},
    "text": "Plan a lighter week every fourth to sixth week.",
}


def _searching(cite: bool, say: str = "Bompa suggests a lighter week every 4–6 weeks.") -> FunctionModel:
    """Searches, then cites every passage it got when `cite`, then says `say`."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = _returns(messages)
        if not returns:
            return ModelResponse(parts=[ToolCallPart("search_knowledge", {"query": "deload"})])
        if cite and len(returns) == 1:
            found = json.loads(returns[0])
            ids = [hit["chunk_id"] for hit in (found["data"] or {"hits": []})["hits"]]
            return ModelResponse(parts=[ToolCallPart("cite_sources", {"chunk_ids": ids})])
        return ModelResponse(parts=[TextPart(say)])

    return FunctionModel(respond)


async def test_a_reply_citing_the_expected_source_passes(tmp_path: Path) -> None:
    root = _set_with(
        tmp_path,
        _case(
            {
                "knowledge": [_DELOAD_PASSAGE],
                "expect": {
                    "called": ["search_knowledge", "cite_sources"],
                    "cites": ["Periodization"],
                },
            }
        ),
    )

    (result,) = await _run(root, _searching(cite=True))

    assert result.passed, result.detail


async def test_a_reply_that_cites_nothing_fails_a_case_expecting_a_source(tmp_path: Path) -> None:
    root = _set_with(
        tmp_path, _case({"knowledge": [_DELOAD_PASSAGE], "expect": {"cites": ["Periodization"]}})
    )

    (result,) = await _run(root, _searching(cite=False))

    assert not result.passed
    assert "cites []" in result.detail


async def test_a_reply_citing_a_source_fails_a_case_expecting_none(tmp_path: Path) -> None:
    root = _set_with(tmp_path, _case({"knowledge": [_DELOAD_PASSAGE], "expect": {"cites": []}}))

    (result,) = await _run(root, _searching(cite=True))

    assert not result.passed
    assert "Periodization" in result.detail


async def test_a_case_with_no_passages_searches_an_empty_knowledge_base(tmp_path: Path) -> None:
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = _returns(messages)
        if not returns:
            return ModelResponse(parts=[ToolCallPart("search_knowledge", {"query": "deload"})])
        seen.extend(returns)
        return ModelResponse(parts=[TextPart("I have no source for that yet.")])

    root = _set_with(tmp_path, _case({"expect": {"cites": []}}))

    (result,) = await _run(root, FunctionModel(respond))

    assert result.passed, result.detail
    assert '"success": false' in seen[0]


async def test_a_case_that_does_not_grade_cites_ignores_them(tmp_path: Path) -> None:
    root = _set_with(tmp_path, _case({"knowledge": [_DELOAD_PASSAGE], "expect": {}}))

    (result,) = await _run(root, _searching(cite=True))

    assert result.passed, result.detail
