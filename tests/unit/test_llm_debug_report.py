"""`ai-trainer llm-debug-report` (ticket #179): the debug log of #178, summed per template and
model, with each prompt part's size in characters (ADR-0018)."""

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_trainer.llm import debug_report
from ai_trainer.llm.debug_report import DebugLogLine, PromptParts

_TOOL = {"name": "final_result", "parameters": {"type": "object"}}


def _line(
    call: str,
    template_id: str = "router",
    *,
    version: int = 2,
    model: str = "z-ai/glm-5.3-flash",
    attempt: int = 1,
    at: str = "2026-10-18T10:00:00+00:00",
    streaming: bool = False,
    latency_ms: int = 1000,
    ttft_ms: int | None = None,
    input_tokens: int = 1000,
    output_tokens: int = 40,
    cost: float | None = 0.001,
    error: str | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "at": at,
        "call": call,
        "template_id": template_id,
        "template_version": version,
        "model": model,
        "attempt": attempt,
        "streaming": streaming,
        "instructions": "i" * 1000,
        "messages": messages if messages is not None else [_request(_prompt("sent 6b"))],
        "output_schema": {"mode": "tool", "object": None, "tools": [_TOOL]},
        "reply": None,
        "response_model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": cost,
        "latency_ms": latency_ms,
        "ttft_ms": ttft_ms,
        "partial_reply": None,
        "error": error,
    }


def _prompt(content: str) -> dict[str, Any]:
    return {"part_kind": "user-prompt", "content": content}


def _request(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "request", "parts": list(parts)}


def _response(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "response", "parts": list(parts)}


def _write(directory: Path, *lines: dict[str, Any] | str, name: str = "2026-10-18.jsonl") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / name).open("a", encoding="utf-8") as file:
        for line in lines:
            file.write((line if isinstance(line, str) else json.dumps(line)) + "\n")


def _report(directory: Path, since: datetime | None = None) -> tuple[int, str]:
    output = io.StringIO()
    code = debug_report.report(directory, since=since, output=output)
    return code, output.getvalue()


def _row(text: str, template: str) -> list[str]:
    """The first table's row for `template`, split on whitespace."""
    return next(line for line in text.splitlines() if line.startswith(template)).split()


def _fixture_lines() -> list[dict[str, Any]]:
    retry_history = [
        _request(_prompt("sent 6b")),
        _response({"part_kind": "text", "content": "bad"}),
        _request({"part_kind": "retry-prompt", "content": "fix it"}),
    ]
    return [
        # router v2: two answered calls and one cut off by the timeout.
        _line("a", latency_ms=800, input_tokens=1000, output_tokens=40, cost=0.001),
        _line("b", latency_ms=1200, input_tokens=1100, output_tokens=50, cost=0.002),
        _line("c", latency_ms=30000, input_tokens=0, output_tokens=0, cost=None,
              error="CancelledError"),
        # climbing.extract v1: an output-validation retry, then a provider error.
        _line("d", "climbing.extract", version=1, latency_ms=900, input_tokens=2000,
              output_tokens=100, cost=0.003),
        _line("d", "climbing.extract", version=1, attempt=2, latency_ms=600, input_tokens=2300,
              output_tokens=120, cost=0.004, messages=retry_history),
        _line("e", "climbing.extract", version=1, latency_ms=100, input_tokens=0,
              output_tokens=0, cost=None, error="ModelHTTPError"),
        # chitchat v1: a streamed reply.
        _line("f", "chitchat", version=1, streaming=True, latency_ms=1500, ttft_ms=300,
              input_tokens=1500, output_tokens=200, cost=0.002),
        # safety_screen v1: a Jev decision, whose provider reports no cost.
        _line("g", "safety_screen", version=1, model="typesafe/jev-1.13", latency_ms=500,
              input_tokens=400, output_tokens=20, cost=None),
    ]  # fmt: skip


_EXPECTED_TABLE = """\
template             model               calls  req/call  p50 ms  p95 ms  p50 ttft ms  in mean  in max  out mean  out max      cost  timeouts  errors
chitchat v1          z-ai/glm-5.3-flash      1      1.00    1500    1500          300     1500    1500       200      200  0.002000         0       0
climbing.extract v1  z-ai/glm-5.3-flash      2      1.50     100    1500            —     2150    4300       110      220  0.007000         0       1
router v2            z-ai/glm-5.3-flash      3      1.00    1200   30000            —      700    1100        30       50  0.003000         1       0
safety_screen v1     typesafe/jev-1.13       1      1.00     500     500            —      400     400        20       20         —         0       0
timeouts: requests cut off by the call timeout or a closed stream (both log CancelledError)

prompt characters per request (mean)
template             requests  instructions  history  message  output schema
chitchat v1                 1          1000        0        7             17
climbing.extract v1         3          1000        3        7             17
router v2                   3          1000        0        7             17
safety_screen v1            1          1000        0        7             17

skipped 0 malformed lines
"""  # noqa: E501


def test_a_fixture_log_gives_the_expected_table(tmp_path: Path) -> None:
    _write(tmp_path, *_fixture_lines())

    assert _report(tmp_path) == (0, _EXPECTED_TABLE)


def test_lines_of_every_daily_file_are_read(tmp_path: Path) -> None:
    _write(tmp_path, _line("a"), name="2026-10-17.jsonl")
    _write(tmp_path, _line("b"), name="2026-10-18.jsonl")

    code, text = _report(tmp_path)

    assert code == 0
    assert _row(text, "router v2")[3] == "2"


def test_since_leaves_out_older_lines(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _line("old", at="2026-10-18T09:59:59+00:00", latency_ms=9000),
        _line("new", at="2026-10-18T10:00:00+00:00", latency_ms=700),
    )

    code, text = _report(tmp_path, since=datetime(2026, 10, 18, 10, tzinfo=UTC))

    assert code == 0
    assert _row(text, "router v2")[3:7] == ["1", "1.00", "700", "700"]


def test_since_keeps_a_line_exactly_at_the_cutoff(tmp_path: Path) -> None:
    _write(tmp_path, _line("a", at="2026-10-18T10:00:00+00:00"))

    code, text = _report(tmp_path, since=datetime(2026, 10, 18, 10, tzinfo=UTC))

    assert (code, _row(text, "router v2")[3]) == (0, "1")


def test_since_keeps_or_leaves_out_a_call_whole_by_its_first_request(tmp_path: Path) -> None:
    _write(
        tmp_path,
        _line("straddles", at="2026-10-18T09:59:59+00:00", latency_ms=5000,
              error="CancelledError"),
        _line("straddles", attempt=2, at="2026-10-18T10:00:01+00:00", latency_ms=1000),
        _line("after", at="2026-10-18T10:00:02+00:00", latency_ms=700),
    )  # fmt: skip

    code, text = _report(tmp_path, since=datetime(2026, 10, 18, 10, tzinfo=UTC))

    assert code == 0
    assert _row(text, "router v2")[3:7] == ["1", "1.00", "700", "700"]


def test_since_with_nothing_newer_says_so_and_exits_1(tmp_path: Path) -> None:
    _write(tmp_path, _line("old", at="2026-10-18T09:00:00+00:00"))

    code, text = _report(tmp_path, since=datetime(2026, 10, 18, 10, tzinfo=UTC))

    assert code == 1
    assert text == (
        f"no debug log lines at {tmp_path} since 2026-10-18T10:00:00+00:00\n"
        "\nskipped 0 malformed lines\n"
    )


def test_a_malformed_line_is_skipped_and_counted_in_the_footer(tmp_path: Path) -> None:
    missing_call = _line("x")
    del missing_call["call"]
    _write(tmp_path, _line("a"), "{not json", missing_call, '["a list"]', "")

    code, text = _report(tmp_path)

    assert code == 0
    assert _row(text, "router v2")[3] == "1"
    assert text.endswith("\nskipped 3 malformed lines\n")


def test_a_line_that_is_not_utf_8_is_skipped_and_counted(tmp_path: Path) -> None:
    _write(tmp_path, _line("a"))
    with (tmp_path / "2026-10-18.jsonl").open("ab") as file:
        file.write(b"\xff\xfe bad\n")

    code, text = _report(tmp_path)

    assert (code, _row(text, "router v2")[3]) == (0, "1")
    assert text.endswith("\nskipped 1 malformed line\n")


def test_a_folder_named_like_a_log_is_ignored(tmp_path: Path) -> None:
    _write(tmp_path, _line("a"))
    (tmp_path / "2026-10-19.jsonl").mkdir()

    assert _report(tmp_path)[0] == 0


def test_an_unreadable_file_is_named_and_the_rest_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, _line("a"), name="2026-10-17.jsonl")
    _write(tmp_path, _line("b"), name="2026-10-18.jsonl")
    read_bytes = Path.read_bytes

    def refuse_one(path: Path) -> bytes:
        if path.name == "2026-10-18.jsonl":
            raise PermissionError(13, "Permission denied")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", refuse_one)

    code, text = _report(tmp_path)

    assert code == 0
    assert text.startswith("could not read 2026-10-18.jsonl (Permission denied)\n")
    assert _row(text, "router v2")[3] == "1"


def test_only_malformed_lines_say_so_and_exit_1(tmp_path: Path) -> None:
    _write(tmp_path, "{not json")

    code, text = _report(tmp_path)

    assert code == 1
    assert text == f"no debug log lines at {tmp_path}\n\nskipped 1 malformed line\n"


def test_a_missing_log_prints_no_debug_log_and_exits_1(tmp_path: Path) -> None:
    missing = tmp_path / ".llm-debug"

    assert _report(missing) == (1, f"no debug log at {missing}\n")


def test_an_empty_log_prints_no_debug_log_and_exits_1(tmp_path: Path) -> None:
    _write(tmp_path, "", "   ")
    (tmp_path / "notes.txt").write_text("not a log line\n")

    assert _report(tmp_path) == (1, f"no debug log at {tmp_path}\n")


def _parts(instructions: str | None, messages: list[dict[str, Any]], **schema: Any) -> PromptParts:
    line = DebugLogLine.model_validate(
        _line("a", messages=messages)
        | {
            "instructions": instructions,
            "output_schema": {"mode": "tool", "object": None, "tools": []} | schema,
        }
    )
    return debug_report.prompt_parts(line)


def test_history_and_message_sections_are_taken_out_of_the_instructions() -> None:
    instructions = (
        "rules\n## Recent messages\n- athlete: hi\n- trainer: hey\n"
        "## Latest message\nsent 6b\n## Rules\nmore"
    )

    parts = _parts(instructions, [_request(_prompt("sent 6b"))])

    history = len("- athlete: hi\n- trainer: hey")
    assert parts == PromptParts(
        instructions=len(instructions) - history - len("sent 6b"),
        history=history,
        message=2 * len("sent 6b"),
        output_schema=0,
    )


def test_the_extraction_v2_heading_counts_as_history_too() -> None:
    """Extraction v2 names its history after the athlete (#190); it is still history."""
    instructions = "rules\n## The athlete's recent messages\n- 3 6b\n## Latest message\n2 more"

    parts = _parts(instructions, [])

    assert (parts.history, parts.message) == (len("- 3 6b"), len("2 more"))
    assert parts.instructions == len(instructions) - len("- 3 6b") - len("2 more")


def test_an_empty_history_and_the_blank_lines_around_sections_are_instructions() -> None:
    instructions = "rules\n\n## Recent messages\n\n\n## Latest message\nsent 6b\n\n## Rules\nmore"

    parts = _parts(instructions, [])

    assert (parts.instructions, parts.history, parts.message) == (
        len(instructions) - len("sent 6b"),
        0,
        len("sent 6b"),
    )


def test_earlier_messages_count_as_history_and_the_last_user_prompt_as_the_message() -> None:
    messages = [
        _request(_prompt("first")),
        _response({"part_kind": "tool-call", "args": {"a": 1}}),
        _request({"part_kind": "tool-return", "content": {"ok": True}}, _prompt("second")),
    ]

    parts = _parts(None, messages)

    assert parts == PromptParts(
        instructions=0,
        history=len("first") + len('{"a":1}') + len('{"ok":true}'),
        message=len("second"),
        output_schema=0,
    )


def test_output_schema_counts_the_object_and_tool_schemas_compactly() -> None:
    parts = _parts(None, [], object={"type": "object", "properties": {}}, tools=[_TOOL, _TOOL])

    assert parts.output_schema == len('{"type":"object","properties":{}}') + 2 * len(
        '{"type":"object"}'
    )


@pytest.mark.parametrize(
    ("values", "p50", "p95"),
    [([5], 5, 5), ([1, 2], 1, 2), ([3, 1, 2], 2, 3), (list(range(1, 21)), 10, 19)],
)
def test_percentiles_use_the_nearest_rank(values: list[int], p50: int, p95: int) -> None:
    assert (debug_report.percentile(values, 50), debug_report.percentile(values, 95)) == (p50, p95)
