import os
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.util._once import Once
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
# Starlette's test client sends `Host: testserver`, which the trusted-host check admits only for
# this origin (ticket #216).
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
# A developer's `.env` may turn the debug log on, which `Settings` refuses for any host but
# localhost (#278); tests never write it unless they ask.
os.environ["LLM_DEBUG_LOG"] = "false"
os.environ["ROUTER_MODEL"] = "test/router-model"
os.environ["CHITCHAT_MODEL"] = "test/chitchat-model"
os.environ["EXTRACT_MODEL"] = "test/extract-model"


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
    # `create_app` with tracing on patches SQLAlchemy and httpx2 process-wide and sets the
    # global tracer provider, which OpenTelemetry allows only once per process (#271).
    for instrumentor in (SQLAlchemyInstrumentor(), HTTPX2ClientInstrumentor()):
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    trace._TRACER_PROVIDER = None


@pytest.fixture(autouse=True)
def _no_jev_model_left_configured() -> Iterator[None]:
    """`JevCheck`'s model is module state that `ai-trainer-evals`' `main()` sets to a real
    `TypeSafeModel`; each test starts and ends with none, so no test inherits it (#124)."""
    from ai_trainer.llm.evals import jev_check

    jev_check._configured = None
    yield
    jev_check._configured = None
