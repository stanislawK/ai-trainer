# ADR 0002 — Runtime and tooling

| | |
|---|---|
| Status | Proposed |
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
- ⚠ Confirm the FastAPI minimum for PEP 649 at M0: the 0.128.1 figure comes from the FastAPI GitHub PR #14789, not from context7.
- Exact version pins are resolved by `uv add` at M0.
- `.claude/rules/python.md` carries these rules.
