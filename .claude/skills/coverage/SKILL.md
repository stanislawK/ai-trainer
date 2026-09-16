---
name: coverage
description: After tests are green, raise line coverage of the modules touched by the current change to 100%.
---

# Coverage on touched modules (ADR-0013)

1. List the touched modules: `git diff --name-only main...HEAD -- 'src/**/*.py'`, plus uncommitted changes from `git status --short`.
2. Measure: `uv run pytest --cov=<module> --cov-report=term-missing` for each module.
3. Cover each missing line through behaviour (the public API), not by testing private details.
4. Delete unreachable code rather than testing it. Use `# pragma: no cover` only for a genuinely untestable line, with a reason.
5. Report the coverage per module. Never lower the global floor.
