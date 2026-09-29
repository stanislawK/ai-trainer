import os
from collections.abc import Iterator

import pytest
from pydantic_ai import Agent, models

# No test may reach a real model (ADR-0013 invariant 1).
models.ALLOW_MODEL_REQUESTS = False

# `Settings` refuses a blank Google sign-in key at startup (ADR-0005), and a developer's local
# `.env` may leave them blank. Set at import time, not in a fixture, so session-scoped
# fixtures that build `Settings()` see them too. Nothing here ever reaches Google.
os.environ["GOOGLE_CLIENT_ID"] = "test-client-id.apps.googleusercontent.com"
os.environ["GOOGLE_CLIENT_SECRET"] = "test-client-secret"
os.environ["SESSION_SECRET_KEY"] = "test-session-secret-key"
os.environ["CSRF_SECRET_KEY"] = "test-csrf-secret-key"


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
