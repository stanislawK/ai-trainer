"""The onboarding's background reads (ADR-0028): `asyncio` tasks the app holds, because the
event loop keeps only weak references to tasks, so an unheld one can vanish mid-read. The
summary awaits the one still running for an answer; shutdown cancels them all."""

import asyncio
import logging
from collections.abc import Coroutine
from uuid import UUID

from ai_trainer.domain.onboarding import OnboardingStep

logger = logging.getLogger(__name__)

type ReadKey = tuple[UUID, OnboardingStep, str | None]


class BackgroundReads:
    def __init__(self) -> None:
        # Every task until it ends, so none is collected; an earlier read of an answer stays
        # held after a newer one starts, and its stale result is dropped by the revision check.
        self._held: set[asyncio.Task[None]] = set()
        self._latest: dict[ReadKey, asyncio.Task[None]] = {}

    def start(self, key: ReadKey, read: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(read)
        self._held.add(task)
        self._latest[key] = task
        task.add_done_callback(lambda done: self._forget(key, done))

    def _forget(self, key: ReadKey, task: asyncio.Task[None]) -> None:
        self._held.discard(task)
        if self._latest.get(key) is task:
            del self._latest[key]
        if not task.cancelled() and (error := task.exception()) is not None:
            # A read never raises into a request; one that fails leaves its answer for the
            # summary to read again.
            logger.error("onboarding read failed", exc_info=error)

    def running(self, key: ReadKey) -> asyncio.Task[None] | None:
        """The newest read of the answer at `key`, while it runs."""
        return self._latest.get(key)

    async def drain(self) -> None:
        """Waits for every held read to end."""
        while self._held:
            await asyncio.gather(*self._held, return_exceptions=True)

    async def close(self) -> None:
        """Cancels every held read, at lifespan shutdown."""
        for task in self._held:
            task.cancel()
        await self.drain()
