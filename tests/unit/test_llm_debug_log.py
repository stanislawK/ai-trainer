import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from pydantic import PostgresDsn, SecretStr
from pydantic_ai import ModelMessage, ModelResponse, TextPart, models
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_trainer.domain.llm_calls import LlmCallOutcome, NewLlmCall
from ai_trainer.llm.debug_log import LlmDebugLog, build_debug_log
from ai_trainer.llm.decision_gateway import OpenRouterDecisionGateway
from ai_trainer.llm.gateway import OpenRouterGateway
from ai_trainer.settings import Settings
from tests.chat_support import FROZEN_NOW, FixedClock
from tests.unit.test_decision_gateway import STATE, FakeJev, _refuse, _sample
from tests.unit.test_llm_gateway import FakeLlmCallsRepository, Greeting


def _settings(directory: Path, *, enabled: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        database_url=PostgresDsn("postgresql+psycopg://u:p@localhost:5432/db"),
        openrouter_api_key=SecretStr("test-key"),
        eval_judge_model="test/judge-model",
        llm_debug_log=enabled,
        llm_debug_log_dir=directory,
    )


def _lines(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted(directory.glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def _invalid_then_valid(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if len(messages) == 1:
        return ModelResponse(
            parts=[TextPart('{"not_text": "oops"}')], provider_details={"cost": 0.1}
        )
    return ModelResponse(parts=[TextPart('{"text": "6b"}')], provider_details={"cost": 0.2})


async def _scripted_stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
    for chunk in ("Nice ", "send"):
        yield chunk


async def _run_climbing_extract(gateway: OpenRouterGateway, user_id: Any) -> None:
    with gateway.agent.override(model=FunctionModel(_invalid_then_valid)):
        await gateway.run(
            user_id=user_id,
            template_id="climbing.extract",
            template_version=3,
            model_id="z-ai/glm-5.3-flash",
            output_type=Greeting,
            instructions="Extract the session.",
            prompt="sent 6b",
        )


@pytest.fixture(autouse=True)
def _allow_fake_decision_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)


def _per_call(lines: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    calls: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        calls.setdefault(line["call"], []).append(line)
    return calls


def _matches(row: NewLlmCall, lines: list[dict[str, Any]]) -> None:
    assert sum(line["input_tokens"] for line in lines) == row.input_tokens
    assert sum(line["output_tokens"] for line in lines) == row.output_tokens
    # The row times the whole call (setup, validation, the retry prompt), each line only its
    # own request, so the lines add up to at most the row's latency.
    assert 0 <= sum(line["latency_ms"] for line in lines) <= row.latency_ms
    assert {line["template_id"] for line in lines} == {row.template_id}
    assert {line["template_version"] for line in lines} == {row.template_version}


async def test_a_climbing_logs_lines_summed_per_call_match_its_llm_calls_rows(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "llm-debug"
    debug_log = build_debug_log(_settings(directory), FixedClock())
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(_settings(directory), repository, debug_log=debug_log)
    decisions = OpenRouterDecisionGateway(
        _settings(directory),
        repository,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(_refuse)),
        debug_log=debug_log,
    )
    user_id = uuid4()

    with decisions.agent.override(model=FakeJev()):
        await decisions.decide(
            user_id=user_id,
            decision_id="sample",
            decision_version=1,
            output_type=_sample(),
            state=STATE,
        )
    await _run_climbing_extract(gateway, user_id)

    decide_row, extract_row = repository.recorded
    decide_lines, extract_lines = _per_call(_lines(directory)).values()
    _matches(decide_row, decide_lines)
    _matches(extract_row, extract_lines)
    assert sum(line["cost"] for line in extract_lines) == pytest.approx(0.3)
    assert decide_lines[0]["messages"]
    assert STATE in json.dumps(decide_lines[0]["messages"])
    assert decide_lines[0]["output_schema"]
    assert extract_lines[0]["model"] == "z-ai/glm-5.3-flash"
    assert extract_lines[0]["instructions"] == "Extract the session."
    assert extract_lines[0]["at"] == FROZEN_NOW.isoformat()
    assert extract_lines[0]["output_schema"]["tools"] or extract_lines[0]["output_schema"]["object"]


async def test_an_output_validation_retry_writes_attempts_1_and_2(tmp_path: Path) -> None:
    directory = tmp_path / "llm-debug"
    gateway = OpenRouterGateway(
        _settings(directory),
        FakeLlmCallsRepository(),
        debug_log=build_debug_log(_settings(directory), FixedClock()),
    )

    await _run_climbing_extract(gateway, uuid4())

    first, second = _lines(directory)
    assert first["call"] == second["call"]
    assert (first["attempt"], second["attempt"]) == (1, 2)
    assert "not_text" in json.dumps(first["reply"])
    assert "6b" in json.dumps(second["reply"])
    assert (first["cost"], second["cost"]) == (0.1, 0.2)

    # The retry carries the validation error back to the model.
    def retry_prompts(line: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            part
            for message in line["messages"]
            for part in message["parts"]
            if part["part_kind"] == "retry-prompt"
        ]

    assert not retry_prompts(first)
    assert retry_prompts(second)


async def test_a_streamed_replys_line_has_its_time_to_first_token_and_text(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "llm-debug"
    gateway = OpenRouterGateway(
        _settings(directory),
        FakeLlmCallsRepository(),
        debug_log=build_debug_log(_settings(directory), FixedClock()),
    )

    with gateway.agent.override(model=FunctionModel(stream_function=_scripted_stream)):
        async with aclosing(
            gateway.stream(
                user_id=uuid4(),
                template_id="chitchat",
                template_version=2,
                model_id="z-ai/glm-5.3-flash",
                instructions="Reply.",
                prompt="hi",
            )
        ) as stream:
            async for _ in stream:
                pass

    [line] = _lines(directory)
    assert line["streaming"] is True
    assert line["attempt"] == 1
    assert line["ttft_ms"] is not None
    assert 0 <= line["ttft_ms"] <= line["latency_ms"]
    assert "Nice send" in json.dumps(line["reply"])
    assert line["partial_reply"] is None


async def test_with_the_flag_off_no_file_is_created(tmp_path: Path) -> None:
    directory = tmp_path / "llm-debug"
    settings = _settings(directory, enabled=False)
    debug_log = build_debug_log(settings, FixedClock())
    gateway = OpenRouterGateway(settings, FakeLlmCallsRepository(), debug_log=debug_log)

    assert debug_log is None
    await _run_climbing_extract(gateway, uuid4())

    assert not directory.exists()


async def test_an_unwritable_folder_fails_neither_the_reply_nor_the_llm_calls_row(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    directory = blocker / "llm-debug"  # mkdir under a regular file always fails
    repository = FakeLlmCallsRepository()
    gateway = OpenRouterGateway(
        _settings(directory),
        repository,
        debug_log=build_debug_log(_settings(directory), FixedClock()),
    )

    with gateway.agent.override(model=FunctionModel(_invalid_then_valid)):
        result = await gateway.run(
            user_id=uuid4(),
            template_id="climbing.extract",
            template_version=3,
            model_id="z-ai/glm-5.3-flash",
            output_type=Greeting,
            instructions="Extract the session.",
            prompt="sent 6b",
        )

    assert result.outcome is LlmCallOutcome.SUCCESS
    [row] = repository.recorded
    assert row.outcome is LlmCallOutcome.SUCCESS
    assert not directory.exists()


async def test_a_line_carries_no_user_id(tmp_path: Path) -> None:
    directory = tmp_path / "llm-debug"
    gateway = OpenRouterGateway(
        _settings(directory),
        FakeLlmCallsRepository(),
        debug_log=build_debug_log(_settings(directory), FixedClock()),
    )
    user_id = uuid4()

    await _run_climbing_extract(gateway, user_id)

    lines = _lines(directory)
    assert lines
    for line in lines:
        assert "user_id" not in line
        assert str(user_id) not in json.dumps(line)


async def test_a_failed_request_writes_its_error_type(tmp_path: Path) -> None:
    def _raise(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("upstream 502")

    directory = tmp_path / "llm-debug"
    gateway = OpenRouterGateway(
        _settings(directory),
        FakeLlmCallsRepository(),
        debug_log=build_debug_log(_settings(directory), FixedClock()),
    )

    with gateway.agent.override(model=FunctionModel(_raise)):
        await gateway.run(
            user_id=uuid4(),
            template_id="climbing.extract",
            template_version=3,
            model_id="z-ai/glm-5.3-flash",
            output_type=Greeting,
            instructions="Extract the session.",
            prompt="sent 6b",
        )

    [line] = _lines(directory)
    assert line["error"] == "RuntimeError"
    assert line["reply"] is None


def test_the_log_writes_one_file_per_utc_day(tmp_path: Path) -> None:
    debug_log = LlmDebugLog(tmp_path, FixedClock())

    assert debug_log.path == tmp_path / f"{FROZEN_NOW.date().isoformat()}.jsonl"


async def test_a_request_cut_off_by_the_call_timeout_still_writes_its_line(
    tmp_path: Path,
) -> None:
    async def invalid_then_hang(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[TextPart('{"not_text": "oops"}')], provider_details={"cost": 0.1}
            )
        await asyncio.Event().wait()  # hangs until the call timeout cancels it
        return ModelResponse(parts=[TextPart('{"text": "too late"}')])

    directory = tmp_path / "llm-debug"
    # The timeout covers both attempts, so the first must finish well inside it: 0.2 s let a
    # slow CI runner cut off attempt 1 instead (#243). Attempt 2 waits out the whole second.
    settings = _settings(directory).model_copy(update={"llm_call_timeout_seconds": 1.0})
    repository = FakeLlmCallsRepository()
    debug_log = LlmDebugLog(directory, FixedClock())
    gateway = OpenRouterGateway(settings, repository, debug_log=debug_log)

    with gateway.agent.override(model=FunctionModel(invalid_then_hang)):
        result = await gateway.run(
            user_id=uuid4(),
            template_id="climbing.extract",
            template_version=3,
            model_id="z-ai/glm-5.3-flash",
            output_type=Greeting,
            instructions="Extract the session.",
            prompt="sent 6b",
        )

    assert result.outcome is LlmCallOutcome.TIMEOUT
    # The cut-off request's line is written in a shielded task the call doesn't wait for.
    await debug_log.drained()
    first, second = _lines(directory)
    assert (first["attempt"], first["error"]) == (1, None)
    assert (second["attempt"], second["error"]) == (2, "CancelledError")
    assert second["reply"] is None


async def test_a_stream_the_consumer_closes_early_still_writes_its_line(
    tmp_path: Path,
) -> None:
    async def chunk_then_stall(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        yield "Nice "
        await asyncio.sleep(10)
        yield "never"

    directory = tmp_path / "llm-debug"
    debug_log = LlmDebugLog(directory, FixedClock())
    gateway = OpenRouterGateway(_settings(directory), FakeLlmCallsRepository(), debug_log=debug_log)

    with gateway.agent.override(model=FunctionModel(stream_function=chunk_then_stall)):
        async with aclosing(
            gateway.stream(
                user_id=uuid4(),
                template_id="chitchat",
                template_version=2,
                model_id="z-ai/glm-5.3-flash",
                instructions="Reply.",
                prompt="hi",
            )
        ) as stream:
            async for _ in stream:
                break

    await debug_log.drained()
    [line] = _lines(directory)
    assert line["error"] == "CancelledError"
    assert line["ttft_ms"] is not None
    # What the athlete already saw before the stream was cut.
    assert line["partial_reply"] == "Nice "


class _HeldCutOffLine(LlmDebugLog):
    """Holds a cut-off request's line until the test releases it, as a slow disk would."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory, FixedClock())
        self.release = threading.Event()

    def _write(self, path: Path, text: str) -> None:
        if '"CancelledError"' in text:
            self.release.wait(timeout=5)
        super()._write(path, text)


async def _cut_off_append(debug_log: LlmDebugLog) -> None:
    """Starts a cut-off request's write and cancels the request, leaving the write pending."""
    request = asyncio.create_task(debug_log.append({"call": "c", "error": "CancelledError"}))
    await asyncio.sleep(0)  # let the write start
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request


async def test_a_cut_off_line_can_land_after_the_call_returns_and_drained_waits_for_it(
    tmp_path: Path,
) -> None:
    async def invalid_then_hang(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[TextPart('{"not_text": "oops"}')])
        await asyncio.Event().wait()
        return ModelResponse(parts=[TextPart('{"text": "too late"}')])

    directory = tmp_path / "llm-debug"
    settings = _settings(directory).model_copy(update={"llm_call_timeout_seconds": 1.0})
    debug_log = _HeldCutOffLine(directory)
    gateway = OpenRouterGateway(settings, FakeLlmCallsRepository(), debug_log=debug_log)

    with gateway.agent.override(model=FunctionModel(invalid_then_hang)):
        result = await gateway.run(
            user_id=uuid4(),
            template_id="climbing.extract",
            template_version=3,
            model_id="z-ai/glm-5.3-flash",
            output_type=Greeting,
            instructions="Extract the session.",
            prompt="sent 6b",
        )

    # The call returned without waiting on the held write.
    assert result.outcome is LlmCallOutcome.TIMEOUT
    assert [line["attempt"] for line in _lines(directory)] == [1]

    debug_log.release.set()
    await debug_log.drained()

    first, second = _lines(directory)
    assert (first["attempt"], first["error"]) == (1, None)
    assert (second["attempt"], second["error"]) == (2, "CancelledError")


async def test_drained_returns_at_once_with_nothing_pending(tmp_path: Path) -> None:
    debug_log = LlmDebugLog(tmp_path, FixedClock())

    async with asyncio.timeout(0):
        await debug_log.drained()


async def test_a_pending_write_to_an_unwritable_folder_warns_and_drains_without_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `lint_imports` in test_import_boundaries.py configures logging with existing loggers
    # disabled, which would silence this one for the rest of the session.
    monkeypatch.setattr(logging.getLogger("ai_trainer.llm.debug_log"), "disabled", False)
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    debug_log = _HeldCutOffLine(blocker / "llm-debug")

    with caplog.at_level(logging.WARNING, logger="ai_trainer.llm.debug_log"):
        await _cut_off_append(debug_log)
        drained = asyncio.create_task(debug_log.drained())
        await asyncio.sleep(0.05)
        assert not drained.done()
        assert "LLM debug log not written" not in caplog.text

        debug_log.release.set()
        await drained

    assert "LLM debug log not written" in caplog.text


async def test_cancelling_drained_leaves_the_pending_write_to_finish(tmp_path: Path) -> None:
    debug_log = _HeldCutOffLine(tmp_path)

    await _cut_off_append(debug_log)
    drained = asyncio.create_task(debug_log.drained())
    await asyncio.sleep(0.05)
    drained.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drained

    debug_log.release.set()
    await debug_log.drained()

    [line] = _lines(tmp_path)
    assert line["error"] == "CancelledError"
