from collections.abc import Iterator

import pytest
from pydantic_ai import Agent, models

# No test may reach a real model (ADR-0013 invariant 1).
models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
def _no_real_telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may export real telemetry, regardless of a developer's local `.env` (ADR-0018).

    `Settings()` reads `.env` by default, so a real `OTEL_ENABLED=true` with live Grafana
    credentials would otherwise instrument every agent for the whole session the moment any
    test builds `Settings`/`create_app` from the environment. This mirrors the
    `ALLOW_MODEL_REQUESTS` guard above and additionally restores `Agent`'s global
    instrumentation state so a test that deliberately enables it (with settings passed
    explicitly, which this env override doesn't affect) never leaks into the next test.
    """
    monkeypatch.setenv("OTEL_ENABLED", "false")
    original = Agent._instrument_default
    yield
    Agent._instrument_default = original
