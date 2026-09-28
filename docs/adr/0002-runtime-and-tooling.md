# ADR 0002 — Runtime and tooling

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-16 |
| Related | PRD-0001, ADR-0003, ADR-0013 |

## Context

[PRD-0001](../prd/0001-ai-training-companion.md) needs a typed Python web application with heavy AI integration. The project owner asked for strong typing, SOLID design, reproducible builds and a single fast toolchain.

## Decision

- **Python 3.14.**
- **uv** manages Python, dependencies and commands (`uv run …`). `uv.lock` is committed; CI and Docker use `uv sync --locked`.
- **ruff** for linting and formatting (`ruff check`, `ruff format`). Formatting is not type safety.
- **mypy `--strict`** with the `pydantic.mypy` plugin, configured with `init_forbid_extra`, `init_typed` and `warn_required_dynamic_aliases` set to true.
- **Pydantic v2** (≥ 2.12, first line with Python 3.14 support) validates every boundary.
- **pydantic-settings** holds all configuration, read from the environment and `.env`. `.env.example` is committed; `.env` never is.
- **FastAPI** (≥ 0.128.1) on uvicorn.
- **Docker + docker compose** run the app and PostgreSQL (ADR-0004). The Dockerfile copies a pinned `ghcr.io/astral-sh/uv:<version>` binary, runs `uv sync --locked --no-install-project`, copies the source, then runs `uv sync --locked`.
- **Layout:** package `ai_trainer` in `src/ai_trainer/`, tests in `tests/`, configuration in `pyproject.toml`.
- **Library docs:** look up current APIs with context7 before using a library (`.claude/rules/context7.md`).

### Invariants

1. All configuration goes through one `Settings` class. Nothing else reads `os.environ`. Secrets are never committed.
2. `ruff check`, `ruff format --check` and `mypy --strict` pass on every commit. A `# type: ignore` needs an error code and a reason.
3. Under PEP 649 (lazy annotations in 3.14), types used in FastAPI or Pydantic runtime signatures must not be imported only under `TYPE_CHECKING`.
4. Dependencies are added with `uv add` (never `pip install`), and the lockfile changes in the same commit.

## Consequences

- An M0 ticket creates `pyproject.toml`, `Dockerfile`, `compose.yaml`, `.env.example` and CI.
- FastAPI minimum for PEP 649, verified at M0 (#2): the fix for `TYPE_CHECKING` annotations under PEP 649 ([PR #14789](https://github.com/fastapi/fastapi/pull/14789)) shipped in **0.128.1** (2026-02-04) per the [FastAPI release notes](https://fastapi.tiangolo.com/release-notes/); context7 had no entry. `pyproject.toml` requires `fastapi>=0.128.1`; `uv add` resolved 0.141.1 at M0.
- Exact versions live only in `uv.lock`; this ADR records floors, not pins.
- `.claude/rules/python.md` carries these rules.
- Docker gotcha, found at #3: the Dockerfile's `RUN uv sync` steps run as root, but the final image switches to a non-root user before `CMD`. `uv run <cmd>` re-checks the environment against the lockfile on every invocation and, as that non-root user, cannot write to the root-owned `.venv` it finds stale — it fails with a permission error on container start. Fix: the runtime `CMD` invokes the venv's binary directly (`.venv/bin` is already first on `PATH`), never `uv run`, so nothing re-syncs after the image is built.
- Docker healthcheck gotcha, found at #3: a `HEALTHCHECK`'s own timeout must leave headroom over any timeout the probed route applies internally (e.g. a DB `connect_timeout`) — otherwise a slow-to-fail dependency can time out the healthcheck probe itself before the route gets a chance to respond gracefully, flapping the container's health status for no real reason.
- Dev reload, added at #62: `make start` runs `docker compose watch` against `compose.yaml` plus a dev-only `compose.dev.yaml` that overrides the `app` command with `uvicorn --reload --reload-dir src`. That override file is deliberately not named `compose.override.yaml` — Compose auto-loads that exact name for every bare `docker compose …` invocation, which would have turned `--reload` on for `make up` and CI's e2e job too. `develop.watch` uses the `sync` action on `src/` rather than a bind mount: Watch's own host-side file watcher plus a tar-copy into the container sidesteps the bind-mount/inotify unreliability macOS Docker Desktop is known for, and needs no `uv run` at container start to notice a dependency change (this ADR's non-root-venv gotcha above still applies), only an image rebuild — `develop.watch` also declares a `rebuild` action on `uv.lock`/`pyproject.toml` for exactly that. Verified at #62: Uvicorn 0.53's `--reload` supervisor (`StatReload`) never inspects the reloaded worker's exit code — it only polls `*.py` mtimes and restarts on the next detected change, so a worker that crashes on a syntax/import error doesn't stop it from trying again once the file is fixed. Contrast its `--workers` multiprocess supervisor, which does check the exit code and gives up after a worker exits with code 3 (Uvicorn 0.50.0) — that path isn't in play here since `compose.dev.yaml` never passes `--workers`. Net effect: a bad edit never needs the container, let alone the stack, restarted; fixing the file is enough.
- Dev migrate service, added at #64: `compose.dev.yaml` adds a one-shot `migrate` service (`alembic upgrade head`, same image and `db`-hostname `DATABASE_URL` override as `app`) that `app` depends on with `condition: service_completed_successfully`, merged into the base file's `depends_on.db` — Compose merges `depends_on` across `-f` files by service key rather than replacing the map, so `app` ends up waiting on both. Living only in `compose.dev.yaml` keeps `make up` and CI's e2e job, which never load that file, unaffected. `migrations/env.py` builds a full `Settings()`, so `migrate` needs `env_file: .env` like `app`, even though the command touches neither `OPENROUTER_API_KEY` nor `EVAL_JUDGE_MODEL`; it runs the venv's `alembic` binary directly, not `uv run`, for the same non-root-venv reason as this ADR's Docker gotcha above. Build gotcha, found at #64: `docker compose watch`'s own implicit build (verified against Compose v5.1.2) only reliably retags the image of the service it's watching (`app`) — a same-context sibling with no `develop.watch` section of its own (`migrate`) can be left running a stale, pre-existing image tag, silently missing a migration file added since that tag was last built, even though nothing in the compose files or the Dockerfile looks wrong. `make start` now runs `docker compose build` before `docker compose watch` so every dev-stack image, watched or not, is rebuilt from the current working tree on every invocation.
