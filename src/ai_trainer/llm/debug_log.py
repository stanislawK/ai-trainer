"""Local LLM traffic log behind `LLM_DEBUG_LOG` (ADR-0018, owner's direction 2026-10-14).

One JSON line per model request — what was sent, what came back, tokens, cost and timing — so
a slow or costly call can be taken apart request by request. It holds prompts and replies, so it
stays on the developer's disk: never in traces, never a user ID or email.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai import ModelRequestContext, ModelResponse, RunContext
from pydantic_ai.capabilities import AbstractCapability, WrapModelRequestHandler
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models import ModelRequestParameters

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.settings import Settings

logger = logging.getLogger(__name__)


class LlmDebugLog:
    """Appends lines to `<directory>/<UTC date>.jsonl`, creating the folder on first write.

    A write that fails is logged and dropped: debugging never fails a reply or its `llm_calls`
    row.
    """

    def __init__(self, directory: Path, clock: ClockPort) -> None:
        self._directory = directory
        self._clock = clock
        # Lines still being written after their request was cancelled; held so they finish.
        self._writes: set[asyncio.Task[None]] = set()

    @property
    def path(self) -> Path:
        return self._directory / f"{self._clock.now().date().isoformat()}.jsonl"

    def recorder(self, *, template_id: str, template_version: int, model: str) -> CallRecorder:
        """A capability for one gateway call; pass it to that call's run only."""
        return CallRecorder(
            log=self, template_id=template_id, template_version=template_version, model=model
        )

    async def append(self, line: dict[str, Any]) -> None:
        """Writes in its own task, so a cancelled request still leaves its line (the same
        anyio re-delivery `CallAccountant.shielded` guards against)."""
        task = asyncio.create_task(self._append({"at": self._clock.now().isoformat(), **line}))
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)
        await asyncio.shield(task)

    async def _append(self, line: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._write, self.path, json.dumps(line, default=str))
        except OSError as exc:
            logger.warning("LLM debug log not written to %s: %s", self._directory, exc)

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(text + "\n")


@dataclass
class CallRecorder(AbstractCapability[Any]):
    """Writes one line per model request of a single gateway call, retries included.

    It is handed no user ID or email, so no line can carry one.
    """

    log: LlmDebugLog
    template_id: str
    template_version: int
    model: str
    call: str = field(default_factory=lambda: uuid.uuid4().hex)
    _attempt: int = 0
    _request_started: float = 0.0
    _first_token_at: float | None = None
    _streamed: list[str] = field(default_factory=list[str])

    def streamed(self, text: str) -> None:
        """Called by a streaming gateway with each piece of text the athlete is shown; the
        first one times `ttft_ms`, and all of them survive a cut-off stream as `partial_reply`."""
        if self._first_token_at is None:
            self._first_token_at = time.monotonic()
        self._streamed.append(text)

    async def wrap_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        handler: WrapModelRequestHandler,
    ) -> ModelResponse:
        self._attempt += 1
        self._request_started = time.monotonic()
        self._first_token_at = None
        self._streamed = []
        line: dict[str, Any] = {
            "call": self.call,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "model": self.model,
            "attempt": self._attempt,
            "streaming": request_context.streaming,
            "instructions": _instructions(request_context.model_request_parameters),
            "messages": ModelMessagesTypeAdapter.dump_python(request_context.messages, mode="json"),
            "output_schema": _output_schema(request_context.model_request_parameters),
        }
        try:
            response = await handler(request_context)
        except (Exception, asyncio.CancelledError) as exc:
            # A timeout or a closed stream cancels the request; it still gets its line.
            await self.log.append(
                line
                | self._timing()
                | _no_reply()
                | {"partial_reply": "".join(self._streamed) or None, "error": type(exc).__name__}
            )
            raise
        await self.log.append(
            line | self._timing() | _reply(response) | {"partial_reply": None, "error": None}
        )
        return response

    def _timing(self) -> dict[str, Any]:
        ended = time.monotonic()
        ttft = self._first_token_at
        return {
            "latency_ms": int((ended - self._request_started) * 1000),
            "ttft_ms": None if ttft is None else int((ttft - self._request_started) * 1000),
        }


def _instructions(parameters: ModelRequestParameters) -> str | None:
    parts = parameters.instruction_parts
    return "\n\n".join(part.content for part in parts) if parts else None


def _output_schema(parameters: ModelRequestParameters) -> dict[str, Any]:
    output_object = parameters.output_object
    return {
        "mode": parameters.output_mode,
        "object": output_object.json_schema if output_object is not None else None,
        "tools": [
            {"name": tool.name, "parameters": tool.parameters_json_schema}
            for tool in parameters.output_tools
        ],
    }


def _reply(response: ModelResponse) -> dict[str, Any]:
    [dumped] = ModelMessagesTypeAdapter.dump_python([response], mode="json")
    return {
        "reply": dumped["parts"],
        "response_model": response.model_name,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cost": (response.provider_details or {}).get("cost"),
    }


def _no_reply() -> dict[str, Any]:
    return {
        "reply": None,
        "response_model": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost": None,
    }


def build_debug_log(settings: Settings, clock: ClockPort) -> LlmDebugLog | None:
    """The log when `LLM_DEBUG_LOG` is on; `None` leaves the gateways capturing nothing."""
    return LlmDebugLog(settings.llm_debug_log_dir, clock) if settings.llm_debug_log else None
