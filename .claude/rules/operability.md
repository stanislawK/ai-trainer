---
paths:
  - "README.md"
  - "Makefile"
  - "Dockerfile"
  - "compose.yaml"
  - "compose.prod.yaml"
  - "deploy/**"
  - ".github/workflows/**"
---

# Operability rules

- `README.md` and the `Makefile` are the human entry point: how to build, run and operate the app locally. A ticket that adds or changes a way to do that — a new service, a new local-dev step, a new operational command — updates both in the same change. Don't let them drift from what `Dockerfile`/`compose.yaml` actually do.
- Makefile targets wrap existing `uv run …` / `docker compose …` commands; they don't invent new behaviour. Keep `CLAUDE.md`'s "Commands" section and the Makefile consistent with each other.
- README's "Project structure" section is a convenience snapshot, not the authority — ADR-0003 owns `src/ai_trainer/`'s layout. Don't let README duplicate ADR/PRD content that could drift; link out instead.
- A non-obvious build/runtime gotcha (a wrong Docker volume path, a flag that silently breaks in a container, an upstream library bug worked around in config) belongs in the relevant ADR's Consequences section, not only in a commit message or PR description — see ADR-0002, ADR-0004 and ADR-0013 for the pattern.
- Production (ADR-0023): `compose.prod.yaml` publishes only Caddy's 443, references images by digest, and reads secrets only from the server's env files. Anything under `deploy/` is a committed, reviewable script or unit. Never a step done by hand and not written down.
- Agents never tag, approve a deployment, or connect to the production host (ADR-0023 invariant 1, ADR-0024 invariant 5). The `PreToolUse` hook `.claude/hooks/guard_release.py` enforces it on every Bash call; when a release or deploy command is added, extend the hook and `tests/unit/test_guard_release_hook.py` with it.
