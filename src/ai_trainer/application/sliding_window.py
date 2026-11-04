"""A rolling window of events, keyed by a string (ADR-0029).

The clock is a port (ADR-0014). The window lives in memory: production is one process, and a
restart clears it.
"""

import math
from collections import deque
from datetime import datetime, timedelta

from ai_trainer.application.ports.clock import ClockPort


class SlidingWindow:
    """Allows `limit` events per `window` for each key. A refused event is not recorded.

    Keys come from the request (a cookie, an address), so idle keys are dropped: at most once
    per window, every key with no event left in it is swept. Memory holds only the keys seen
    in the last two windows.
    """

    def __init__(self, clock: ClockPort, *, window: timedelta) -> None:
        self._clock = clock
        self._window = window
        self._stamps: dict[str, deque[datetime]] = {}
        self._swept = clock.now()

    def allow(self, key: str, *, limit: int) -> int | None:
        """Records the event and returns `None`, or the seconds to wait when the key is full.

        The wait is rounded up and at least one second, so a `Retry-After` of zero is never
        sent and a client that waits it out is not refused again.
        """
        retry = self.wait_if_full(key, limit=limit)
        if retry is None:
            self.record(key)
        return retry

    def wait_if_full(self, key: str, *, limit: int) -> int | None:
        """The seconds to wait when the key already holds `limit` events, else `None`.

        Records nothing, for a caller that counts only some outcomes (failed sign-ins).
        """
        now = self._clock.now()
        stamps = self._live(key, now)
        if len(stamps) < limit:
            return None
        retry = math.ceil((stamps[0] + self._window - now).total_seconds())
        return max(retry, 1)

    def record(self, key: str) -> None:
        """Counts one event for the key, whether or not it is full."""
        now = self._clock.now()
        self._live(key, now).append(now)

    def _live(self, key: str, now: datetime) -> deque[datetime]:
        cutoff = now - self._window
        self._sweep(now, cutoff)
        stamps = self._stamps.setdefault(key, deque())
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()
        return stamps

    def __len__(self) -> int:
        return len(self._stamps)

    def _sweep(self, now: datetime, cutoff: datetime) -> None:
        if now - self._swept < self._window:
            return
        self._swept = now
        idle = [key for key, stamps in self._stamps.items() if not stamps or stamps[-1] <= cutoff]
        for key in idle:
            del self._stamps[key]
