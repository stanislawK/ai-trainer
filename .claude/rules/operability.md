---
paths:
  - "README.md"
  - "Makefile"
  - "Dockerfile"
  - "compose.yaml"
---

# Operability rules

- `README.md` and the `Makefile` are the human entry point: how to build, run and operate the app locally. A ticket that adds or changes a way to do that — a new service, a new local-dev step, a new operational command — updates both in the same change. Don't let them drift from what `Dockerfile`/`compose.yaml` actually do.
- Makefile targets wrap existing `uv run …` / `docker compose …` commands; they don't invent new behaviour. Keep `CLAUDE.md`'s "Commands" section and the Makefile consistent with each other.
- README's "Project structure" section is a convenience snapshot, not the authority — ADR-0003 owns `src/ai_trainer/`'s layout. Don't let README duplicate ADR/PRD content that could drift; link out instead.
- A non-obvious build/runtime gotcha (a wrong Docker volume path, a flag that silently breaks in a container, an upstream library bug worked around in config) belongs in the relevant ADR's Consequences section, not only in a commit message or PR description — see ADR-0002, ADR-0004 and ADR-0013 for the pattern.
