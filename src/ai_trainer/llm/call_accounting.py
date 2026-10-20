"""What every model gateway shares: one `llm_calls` row per call, however it ends (ADR-0018)."""

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic_ai import ModelMessage, ModelResponse

from ai_trainer.application.ports.llm_calls import LlmCallsRepositoryPort
from ai_trainer.domain.llm_calls import LlmCallOutcome, NewLlmCall


@dataclass(frozen=True, slots=True)
class CallKey:
    """Who made the call and for what; `model` is the configured ID, used when the call
    produced no response to name one."""

    user_id: UUID
    template_id: str
    template_version: int
    model: str


@dataclass(frozen=True, slots=True)
class CallUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal | None = None
    # The model the response names, when it names a more exact one than the configured ID.
    model: str | None = None


@dataclass(frozen=True, slots=True)
class Attempt[T]:
    value: T | None
    outcome: LlmCallOutcome


class CallAccountant:
    def __init__(self, calls_repository: LlmCallsRepositoryPort, *, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self._calls_repository = calls_repository
        # Rows still being written after their caller was cancelled; held so they finish.
        self._writes: set[asyncio.Task[None]] = set()

    async def attempt[T](
        self,
        key: CallKey,
        call: Coroutine[Any, Any, T],
        *,
        usage: Callable[[T], CallUsage],
        timeout_seconds: float | None = None,
    ) -> Attempt[T]:
        """Awaits `call` under `timeout_seconds` (the accountant's own when unset) and records
        it. Every model or provider failure becomes an outcome, never an exception (ADR-0007);
        a cancelled caller is recorded as `cancelled` and the cancellation goes on."""
        started = time.monotonic()
        try:
            value = await asyncio.wait_for(
                call, timeout=self.timeout_seconds if timeout_seconds is None else timeout_seconds
            )
        except TimeoutError:
            return await self._failed(key, started, LlmCallOutcome.TIMEOUT)
        except asyncio.CancelledError:
            # The caller went away mid-call, e.g. a browser that closed the reply stream.
            await self._failed(key, started, LlmCallOutcome.CANCELLED)
            raise
        except Exception:
            return await self._failed(key, started, LlmCallOutcome.ERROR)
        await self.shielded(self.write(key, usage(value), started, LlmCallOutcome.SUCCESS))
        return Attempt(value, LlmCallOutcome.SUCCESS)

    async def _failed(self, key: CallKey, started: float, outcome: LlmCallOutcome) -> Attempt[Any]:
        await self.shielded(self.write(key, CallUsage(), started, outcome))
        return Attempt(None, outcome)

    async def shielded(self, write: Coroutine[None, None, None]) -> None:
        """Runs `write` in its own task, so cancelling the caller never cancels the row's
        write. anyio, which FastAPI's streaming responses run in, re-delivers a cancellation
        at every await of the cancelled caller; a plain `finally` would be cut short at its
        first await and the call would go unrecorded (ADR-0018 invariant 1, found at #82)."""
        task = asyncio.create_task(write)
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)
        await asyncio.shield(task)

    async def write(
        self, key: CallKey, usage: CallUsage, started: float, outcome: LlmCallOutcome
    ) -> None:
        await self._calls_repository.record(
            NewLlmCall(
                user_id=key.user_id,
                template_id=key.template_id,
                template_version=key.template_version,
                model=usage.model or key.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost=usage.cost,
                latency_ms=int((time.monotonic() - started) * 1000),
                outcome=outcome,
            )
        )


def sum_cost(messages: list[ModelMessage]) -> Decimal | None:
    """Sums `provider_details['cost']` across every model response in the run.

    A single `agent.run()` call can still make more than one model request — Pydantic AI retries
    output validation once by default — so `result.response` (the last response only) would
    silently undercount cost on a retried call. `None` when no response carried a cost at all.

    Each cost is converted to `Decimal` before summing, not after: summing the raw floats first
    (e.g. `0.1 + 0.2 == 0.30000000000000004`) would bake that binary rounding error into the
    stored figure, where converting each addend through its clean decimal `repr` first and only
    then summing avoids it.
    """
    costs: list[Decimal] = []
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        raw_cost = (message.provider_details or {}).get("cost")
        if raw_cost is not None:
            costs.append(Decimal(str(raw_cost)))
    return sum(costs, Decimal(0)) if costs else None
