"""`BackgroundReads` holds the onboarding's read tasks (ADR-0028, ticket #254): the newest per
answer is found while it runs, an earlier one stays held until it ends, and one that raises is
logged rather than lost."""

import asyncio
import logging
from uuid import uuid4

import pytest

from ai_trainer.application.background_reads import BackgroundReads
from ai_trainer.domain.onboarding import OnboardingStep

KEY = (uuid4(), OnboardingStep.ABOUT_YOU, None)


async def test_a_newer_read_is_the_one_found_and_the_earlier_one_still_finishes() -> None:
    reads = BackgroundReads()
    finished: list[str] = []
    gate = asyncio.Event()

    async def read(name: str) -> None:
        await gate.wait()
        finished.append(name)

    reads.start(KEY, read("earlier"))
    reads.start(KEY, read("newer"))
    newer = reads.running(KEY)
    assert newer is not None

    gate.set()
    await reads.drain()

    assert sorted(finished) == ["earlier", "newer"]
    assert reads.running(KEY) is None


async def test_a_read_that_raises_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    reads = BackgroundReads()

    async def read() -> None:
        raise RuntimeError("database gone")

    with caplog.at_level(logging.ERROR, logger="ai_trainer.application.background_reads"):
        reads.start(KEY, read())
        await reads.drain()

    assert "onboarding read failed" in caplog.text
    assert reads.running(KEY) is None
