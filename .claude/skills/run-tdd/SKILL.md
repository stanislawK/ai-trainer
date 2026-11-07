---
name: run-tdd
description: Red-green-refactor loop for each acceptance criterion, followed by coverage, lint, type check and the OpenAPI snapshot. Use while implementing any ticket.
---

# Run TDD (ADR-0013)

For each AC:

1. **Red.** Write the smallest test that expresses the AC, named after the behaviour. Run `uv run pytest <path>::<test> -x -q --no-header --tb=short -n 0` and confirm it fails for the right reason (not an import or syntax error).
2. **Green.** Write the minimum code that makes it pass. Run the test again.
3. **Refactor** with the tests green: names, duplication, layering (ADR-0003). Check the smells ruff caps: complexity over 10, more than 50 statements or 12 branches, more than 5 parameters (make them keyword-only or a parameter object), a bare `Any`.

Then for the whole change:

4. `PYTEST_ADDOPTS='-q --no-header --tb=short' make test-fast` — everything green (unit + integration, parallel, stops at the first failure). On a failure, re-run only that test with `--tb=long`; never re-run the whole suite just to read a traceback. Every line of output stays in context for the rest of the session, so keep it short, and never pipe it through `tail` or `head`, which hides the exit code. Also run `make test-ops` when `deploy/`, `scripts/` or `compose*.yaml` changed. CI runs parallel jobs (`static`, `test`, `ops`, `e2e`) behind one required `ci` check, skipping the ones a pull request's paths don't need (`scripts/ci_changes.py`, #357).
5. `/coverage` — 100% line coverage on the touched modules.
6. `uv run ruff check . && uv run ruff format --check .`
7. `uv run mypy` — strict, clean.
8. If the HTTP surface changed, regenerate the OpenAPI snapshot with the script from M0. Never hand-edit `docs/api/openapi.json`.

Rules: no real model or network calls (`TestModel` / `FunctionModel`); fakes over mocks; never weaken a test to make it pass.
