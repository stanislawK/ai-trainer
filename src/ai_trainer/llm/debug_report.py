"""`ai-trainer llm-debug-report`: the debug log of `LLM_DEBUG_LOG`, summed per template (#179).

Lines are grouped into gateway calls by their `call` id; a call's latency and tokens are the sums
of its lines, as for its `llm_calls` row (ADR-0018). Prompt size is attributed in characters, never
estimated tokens (ADR-0018 invariant 6): every template renders the recent turns and the latest
message into its instructions under `## Recent messages` and `## Latest message`, so those sections
count as history and message rather than instructions.
"""

import json
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from pydantic import AwareDatetime, BaseModel, JsonValue, ValidationError

_SECTION = re.compile(r"^## (Recent messages|Latest message)\n(.*?)(?=\n## |\Z)", re.M | re.S)
_TIMED_OUT = "CancelledError"


class DebugLogLine(BaseModel):
    """The fields of a `CallRecorder` line the report reads; any other key is ignored."""

    at: AwareDatetime
    call: str
    template_id: str
    template_version: int
    model: str
    attempt: int
    streaming: bool
    instructions: str | None
    messages: list[dict[str, JsonValue]]
    output_schema: dict[str, JsonValue]
    input_tokens: int
    output_tokens: int
    cost: float | None
    latency_ms: int
    ttft_ms: int | None
    error: str | None


@dataclass(frozen=True)
class DebugLog:
    lines: list[DebugLogLine]
    malformed: int
    found: bool
    unreadable: list[str]


@dataclass(frozen=True)
class PromptParts:
    instructions: int
    history: int
    message: int
    output_schema: int


def read_debug_log(directory: Path, *, since: datetime | None) -> DebugLog:
    """Every valid line of `<directory>/*.jsonl` from the calls that began at or after `since`
    (a call is kept or left out whole, so a retry across the cutoff isn't halved). A line that
    isn't valid UTF-8 JSON of the expected shape is counted as malformed, a blank one not at all,
    and a file that can't be read is named rather than failing the report."""
    lines: list[DebugLogLine] = []
    malformed = 0
    found = False
    unreadable: list[str] = []
    for path in sorted(path for path in directory.glob("*.jsonl") if path.is_file()):
        try:
            content = path.read_bytes()
        except OSError as exc:
            unreadable.append(f"{path.name} ({exc.strerror})")
            continue
        for raw in content.splitlines():
            if not raw.strip():
                continue
            found = True
            try:
                lines.append(DebugLogLine.model_validate_json(raw.decode("utf-8")))
            except UnicodeDecodeError, ValidationError:
                malformed += 1
    if since is not None:
        began: dict[str, datetime] = {}
        for line in lines:
            began[line.call] = min(began.get(line.call, line.at), line.at)
        lines = [line for line in lines if began[line.call] >= since]
    return DebugLog(lines=lines, malformed=malformed, found=found, unreadable=unreadable)


def report(directory: Path, *, since: datetime | None, output: TextIO) -> int:
    """Writes the report and returns the exit code: 1 when there is nothing to summarize."""
    log = read_debug_log(directory, since=since)
    for name in log.unreadable:
        output.write(f"could not read {name}\n")
    if not log.found:
        output.write(f"no debug log at {directory}\n")
        return 1
    if log.lines:
        output.write(_calls_table(log.lines))
        output.write("\nprompt characters per request (mean)\n")
        output.write(_prompt_table(log.lines))
    else:
        after = f" since {since.isoformat()}" if since is not None else ""
        output.write(f"no debug log lines at {directory}{after}\n")
    plural = "" if log.malformed == 1 else "s"
    output.write(f"\nskipped {log.malformed} malformed line{plural}\n")
    return 0 if log.lines else 1


def prompt_parts(line: DebugLogLine) -> PromptParts:
    instructions = line.instructions or ""
    # The blank lines a template leaves around a section are layout, so they stay instructions.
    sections = {name: len(body.strip()) for name, body in _SECTION.findall(instructions)}
    history = sections.get("Recent messages", 0)
    message = sections.get("Latest message", 0)
    parts = [part for item in line.messages for part in _parts_of(item)]
    last_prompt = max(
        (i for i, part in enumerate(parts) if part.get("part_kind") == "user-prompt"), default=None
    )
    for i, part in enumerate(parts):
        if i == last_prompt:
            message += _chars(part.get("content"))
        else:
            history += _chars(part.get("content", part.get("args")))
    return PromptParts(
        instructions=len(instructions) - sum(sections.values()),
        history=history,
        message=message,
        output_schema=_schema_chars(line.output_schema),
    )


def percentile(values: Sequence[int], p: int) -> int:
    """Nearest rank: the smallest value at least `p` percent of `values` are at or below."""
    ordered = sorted(values)
    return ordered[max(-(-p * len(ordered) // 100), 1) - 1]


def _parts_of(message: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    parts = message.get("parts")
    return [part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else []


def _chars(value: JsonValue) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(_compact(value))


def _schema_chars(schema: dict[str, JsonValue]) -> int:
    tools = schema.get("tools")
    tool_schemas = (
        [tool.get("parameters") for tool in tools if isinstance(tool, dict)]
        if isinstance(tools, list)
        else []
    )
    return sum(_chars(value) for value in [schema.get("object"), *tool_schemas])


def _compact(value: JsonValue) -> str:
    return json.dumps(value, separators=(",", ":"))


def _calls_table(lines: Iterable[DebugLogLine]) -> str:
    calls: dict[str, list[DebugLogLine]] = defaultdict(list)
    for line in lines:
        calls[line.call].append(line)
    groups: dict[tuple[str, int, str], list[list[DebugLogLine]]] = defaultdict(list)
    for requests in calls.values():
        first = requests[0]
        groups[(first.template_id, first.template_version, first.model)].append(
            sorted(requests, key=lambda line: line.attempt)
        )
    header = [
        "template", "model", "calls", "req/call", "p50 ms", "p95 ms", "p50 ttft ms", "in mean",
        "in max", "out mean", "out max", "cost", "timeouts", "errors",
    ]  # fmt: skip
    rows = [
        _call_row(template_id, version, model, groups[(template_id, version, model)])
        for template_id, version, model in sorted(groups)
    ]
    note = (
        "timeouts: requests cut off by the call timeout or a closed stream "
        "(both log CancelledError)"
    )
    return _table(header, rows, text_columns=2) + note + "\n"


def _call_row(
    template_id: str, version: int, model: str, calls: list[list[DebugLogLine]]
) -> list[str]:
    latencies = [sum(line.latency_ms for line in call) for call in calls]
    ttfts = [
        ttft
        for call in calls
        if (ttft := next((line.ttft_ms for line in call if line.ttft_ms is not None), None))
        is not None
    ]
    inputs = [sum(line.input_tokens for line in call) for call in calls]
    outputs = [sum(line.output_tokens for line in call) for call in calls]
    costs = [line.cost for call in calls for line in call if line.cost is not None]
    errors = [call[-1].error for call in calls if call[-1].error is not None]
    return [
        f"{template_id} v{version}",
        model,
        str(len(calls)),
        f"{sum(len(call) for call in calls) / len(calls):.2f}",
        str(percentile(latencies, 50)),
        str(percentile(latencies, 95)),
        str(percentile(ttfts, 50)) if ttfts else "—",
        str(round(sum(inputs) / len(inputs))),
        str(max(inputs)),
        str(round(sum(outputs) / len(outputs))),
        str(max(outputs)),
        f"{sum(costs):.6f}" if costs else "—",
        str(errors.count(_TIMED_OUT)),
        str(len(errors) - errors.count(_TIMED_OUT)),
    ]


def _prompt_table(lines: Iterable[DebugLogLine]) -> str:
    templates: dict[tuple[str, int], list[PromptParts]] = defaultdict(list)
    for line in lines:
        templates[(line.template_id, line.template_version)].append(prompt_parts(line))
    header = ["template", "requests", "instructions", "history", "message", "output schema"]
    rows = []
    for template_id, version in sorted(templates):
        parts = templates[(template_id, version)]
        rows.append(
            [
                f"{template_id} v{version}",
                str(len(parts)),
                *(
                    str(round(sum(getattr(part, name) for part in parts) / len(parts)))
                    for name in ("instructions", "history", "message", "output_schema")
                ),
            ]
        )
    return _table(header, rows, text_columns=1)


def _table(header: list[str], rows: list[list[str]], *, text_columns: int) -> str:
    """The first `text_columns` columns left-aligned, the numbers after them right-aligned."""
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]

    def render(row: list[str]) -> str:
        cells = [
            cell.ljust(width) if i < text_columns else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths, strict=True))
        ]
        return "  ".join(cells).rstrip()

    return "".join(render(row) + "\n" for row in [header, *rows])
