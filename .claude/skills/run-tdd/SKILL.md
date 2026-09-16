---
name: run-tdd
description: Red-green-refactor loop for each acceptance criterion, followed by coverage, lint, type check and the OpenAPI snapshot. Use while implementing any ticket.
---

# Run TDD (ADR-0013)

For each AC:

1. **Red.** Write the smallest test that expresses the AC, named after the behaviour. Run `uv run pytest <path>::<test> -x` and confirm it fails for the right reason (not an import or syntax error).
2. **Green.** Write the minimum code that makes it pass. Run the test again.
3. **Refactor** with the tests green: names, duplication, layering (ADR-0003).

Then for the whole change:

4. `uv run pytest` — everything green.
5. `/coverage` — 100% line coverage on the touched modules.
6. `uv run ruff check . && uv run ruff format --check .`
7. `uv run mypy` — strict, clean.
8. If the HTTP surface changed, regenerate the OpenAPI snapshot with the script from M0. Never hand-edit `docs/api/openapi.json`.

Rules: no real model or network calls (`TestModel` / `FunctionModel`); fakes over mocks; never weaken a test to make it pass.
