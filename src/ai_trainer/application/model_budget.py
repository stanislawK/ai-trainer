"""Per-user cap on model calls (ADR-0029). Text calls and decision calls share one budget."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID

from ai_trainer.application.ports.clock import ClockPort
from ai_trainer.application.ports.metrics import NO_METRICS, MetricsPort
from ai_trainer.application.sliding_window import SlidingWindow

_HOUR = timedelta(hours=1)


class ModelCallBudget:
    """At most `per_hour` calls in a rolling hour, and `in_flight` at once, per user.

    A call that is refused is not counted. A call that starts is counted for the hour even
    when the caller cancels it: dropping the hour's count on cancel would let a loop open
    and abandon calls forever. Cancelling releases only the in-flight slot.
    """

    def __init__(
        self,
        clock: ClockPort,
        *,
        per_hour: int,
        in_flight: int,
        metrics: MetricsPort = NO_METRICS,
    ) -> None:
        self._window = SlidingWindow(clock, window=_HOUR)
        self._metrics = metrics
        self._per_hour = per_hour
        self._in_flight_limit = in_flight
        self._in_flight: dict[UUID, int] = {}

    def try_acquire(self, user_id: UUID) -> bool:
        current = self._in_flight.get(user_id, 0)
        if current >= self._in_flight_limit:
            self._metrics.refusal("model_calls_in_flight")
            return False
        if self._window.allow(str(user_id), limit=self._per_hour) is not None:
            self._metrics.refusal("model_calls_per_hour")
            return False
        self._in_flight[user_id] = current + 1
        return True

    def release(self, user_id: UUID) -> None:
        current = self._in_flight.get(user_id, 0)
        if current <= 1:
            self._in_flight.pop(user_id, None)
        else:
            self._in_flight[user_id] = current - 1

    @asynccontextmanager
    async def hold(self, user_id: UUID) -> AsyncIterator[bool]:
        """Yields whether the call may proceed. The in-flight slot is released on the way out,
        including when the caller is cancelled."""
        allowed = self.try_acquire(user_id)
        try:
            yield allowed
        finally:
            if allowed:
                self.release(user_id)
