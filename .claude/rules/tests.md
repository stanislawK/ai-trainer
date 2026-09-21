---
paths:
  - "tests/**/*.py"
---

# Test rules (ADR-0013)

- Test first: red → green → refactor. Each ticket AC maps to at least one named test.
- `tests/unit/` for domain and application with fakes; `tests/integration/` for real PostgreSQL, adapters, routes and MCP; `tests/e2e/` for pytest-playwright.
- No network. `models.ALLOW_MODEL_REQUESTS = False` is set in `conftest.py`; agents run with `TestModel` or `FunctionModel` via `agent.override`. External route, crag and geo sources are faked through their ports; parsing is tested against recorded fixtures (ADR-0016, ADR-0017).
- Time is frozen through the fake `Clock`; no test reads the wall clock. Cover midnight and DST boundaries, and a message sent at 01:00 about "yesterday evening" (ADR-0014).
- Prefer fakes that implement ports over `mock.patch`.
- Every repository and every user-facing route has a tenancy test: user A cannot read or change user B's data.
- Test MCP tools through FastMCP's in-memory `Client(server)`.
- pytest-asyncio runs in `auto` mode, so async tests need no marker.
- Never weaken a test to make it pass. Evals (`evals/`, ADR-0009) are not tests.
