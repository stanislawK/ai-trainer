# ai-trainer

An AI training companion for amateur athletes (climbing, gym, cycling). Document-driven, AI-first, TDD.

## Status

**M0 (Foundations) in progress.** So far: a typed Python/FastAPI skeleton, a `docker compose` stack (app + PostgreSQL/pgvector) with a `GET /health` endpoint that reports database reachability without needing a session, a styled base layout (`GET /`) built with Jinja2 + htmx 4 + Tailwind CSS v4/daisyUI 5 — htmx is vendored under `static/`, the stylesheet is compiled at image build time with no Node.js anywhere — Google sign-in (`/auth/login`, `/auth/callback`, `/auth/logout`) with approval-gated accounts, every other route gated on an active account, CSRF protection on non-GET requests (ADR-0005), and the Liquid Glass `trainer-dark`/`trainer-light` themes and glass utilities (ADR-0019), dark by default and remembered per browser with no flash on reload.

Not built yet: the rest of the Liquid Glass design foundation (icons, installability, the app shell — ADR-0019), the admin user-list page, account deletion, any product feature. The eval harness (`make evals` / `ai-trainer-evals`, ADR-0009) is wired up but has no datasets to run yet — those ship with the first M1 specialist templates. See [CLAUDE.md](CLAUDE.md) for the full milestone plan and [docs/prd/README.md](docs/prd/README.md) / [docs/adr/README.md](docs/adr/README.md) for the product requirements and the architecture decisions that govern the stack.

## Continuous integration

Every push and pull request runs `ruff check`, `ruff format --check`, `mypy --strict`, the import-boundary check (`import-linter`, ADR-0003), the OpenAPI snapshot drift check and the full `pytest` suite (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)), with integration tests hitting a real PostgreSQL/pgvector service container. Once that passes, a second `e2e` job builds and starts the full `docker compose` stack and runs the pytest-playwright specs in `tests/e2e/` (ADR-0013). Prompt evals never run here — they cost money and are triggered on demand, manually, through the separate `workflow_dispatch`-only [`.github/workflows/evals.yml`](.github/workflows/evals.yml) (ADR-0009).

This project is document-driven: PRDs and ADRs are the source of truth, not this file. If something here ever looks out of date, trust the docs and open a doc fix — see [docs/README.md](docs/README.md).

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) + Docker Compose v2
- [uv](https://docs.astral.sh/uv/) (for running the app or its tests outside a container)
- Python 3.14 (uv will install/manage this for you)

## Quick start

```bash
cp .env.example .env      # local-only defaults; never commit .env
```

Fill in `OPENROUTER_API_KEY` in `.env` — get one at [openrouter.ai/keys](https://openrouter.ai/keys); `Settings` requires it to start even before any LLM feature ships (ADR-0007).

To sign in with Google locally, create an OAuth 2.0 client (type "Web application") at [Google Cloud Console](https://console.cloud.google.com/apis/credentials), with authorized redirect URI `http://localhost:8000/auth/callback`, then fill `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `SESSION_SECRET_KEY` (`openssl rand -hex 32`) in `.env`. Add your own email to `ADMIN_EMAILS` (comma-separated) to bootstrap the first admin account as `active` (ADR-0005) — otherwise every new account stays `pending`. Not needed to run the test suite, only to actually sign in.

```bash
make start                # builds, migrates the schema to head, and starts app + postgres (foreground)
```

In a second terminal: `make health` → `{"database":"ok"}`. Running the app needs only Docker and `.env` — `make start`'s one-shot `migrate` step (#64) brings the schema to head before `app` starts, so there's no separate `make migrate`/local `uv` step for a fresh clone. See "Dev loop" below for what else `make start` does, and `make up` in the table below for a plain background start without live reload (still needs `make migrate` once, or `make test`/`make test-integration`, to have a schema).

The app is then at `http://localhost:8000`, Postgres at `localhost:5432` (credentials in `.env.example`).

## Common commands

Run `make` targets from the repo root; see the [Makefile](Makefile) for the complete, current list (`grep -E '^[a-zA-Z_-]+:' Makefile`). The ones you'll use most:

| Command | What it does |
|---|---|
| `make up` | Start app + postgres (background) |
| `make up-build` | Rebuild images, then start |
| `make start` | Dev loop: app + postgres with live reload (foreground) — see "Dev loop" below |
| `make stop` | Stop the `make start` stack (keeps the postgres volume) |
| `make down` | Stop the stack (keeps the postgres volume) |
| `make logs` | Follow container logs |
| `make ps` | Show container + healthcheck status |
| `make health` | Curl the running app's `/health` |
| `make install` | `uv sync` — install dependencies locally |
| `make css` | Compile the Tailwind CSS v4 + daisyUI 5 stylesheet (no Node) |
| `make test` | Run the full test suite |
| `make test-unit` / `make test-integration` | Run one test tier (integration needs `make up` first) |
| `make e2e-install` | One-time Playwright Chromium browser download |
| `make e2e` | Run the pytest-playwright specs in `tests/e2e/` (needs `make up` first) |
| `make coverage` | Line coverage on `src/ai_trainer` |
| `make migrate` | Apply database migrations (Alembic) |
| `make lint` / `make format` / `make typecheck` | ruff / ruff format / mypy --strict |
| `make import-lint` | Check layer boundaries (import-linter, ADR-0003) |
| `make openapi` | Regenerate the OpenAPI snapshot (`docs/api/openapi.json`) |
| `make openapi-check` | Regenerate, then fail if the committed snapshot drifted (what CI runs) |
| `make check` | Everything CI runs: lint, format check, typecheck, import-lint, openapi-check, tests |
| `make evals template=<id>` | Run a prompt template's eval dataset (ADR-0009) — costs money, no default CI job runs it |

`test`, `test-integration`, `coverage` and `e2e` run `make migrate` first, so the `vector` extension always exists before the suite runs. `make evals` takes an optional `version=` and `model=` to override the template version or the model under test; it needs `EVAL_JUDGE_MODEL` set in `.env` (ADR-0009) and a dataset at `evals/datasets/<id>.yaml`, which the first M1 specialist template ships.

## Dev loop

`make start` runs `docker compose watch` (`compose.yaml` plus the dev-only `compose.dev.yaml`) instead of `make up`'s plain `docker compose up -d --build`: same app + Postgres, but the app container runs `uvicorn --reload` and Compose Watch syncs edited files under `src/` straight into the running container — no image rebuild, no manual restart.

```bash
make start   # foreground: builds, starts, then streams logs + sync/rebuild events
```

It's foreground on purpose (`docker compose watch` can't run detached), so use a second terminal for `make health`, `make logs` or `make ps` while it's running. `make stop` stops it (keeps the Postgres volume, same as `make down`).

- A route's edited Python response and a fixed syntax error both show up within a few seconds — Compose Watch syncs the file, then Uvicorn's own `--reload` notices the change and restarts just the app process (not the container). A syntax error shows as a traceback in the logs; the app keeps retrying and recovers as soon as the file is valid again, no `make stop`/`make start` needed.
- An edited Jinja template shows on the very next request, no restart at all: Jinja's environment reloads a changed template per-request on its own once the file is synced.
- A dependency added with `uv add` needs a fresh image (Compose Watch rebuilds automatically if `uv.lock`/`pyproject.toml` change while `make start` is running, or rebuilds on the next `make start` either way).
- An edited daisyUI class or a `theme.css`/`glass.css` token shows on the next page load too: alongside `uvicorn --reload`, the app container runs a Tailwind `--watch` process that recompiles `app.css` on every synced change — no `make css`, no rebuild (#63). A CSS syntax error shows in `make logs` and the last good `app.css` keeps serving until the file is fixed.
- A one-shot `migrate` service (#64) runs `alembic upgrade head` against Postgres before `app` starts, on every `make start` — a fresh, empty volume reaches head, and a migration pulled since your last `make start` is applied on the next one; an up-to-date schema makes it a fast no-op. A failing migration keeps `app` from starting at all; its traceback shows in `make logs`.
- `make up`, the production image and CI's e2e job never use `--reload`, the CSS watcher, the migrate service or `compose.dev.yaml` — this is purely a local dev loop.

## End-to-end tests

`tests/e2e/` (pytest-playwright, ADR-0013) runs against the full running app, not a test client, so it needs `make up` (or `make up-build`) first, and `make e2e-install` once to download the Chromium browser. Signed-in pages are reached through `scripts/dev_session.py`, a dev-only script that seeds an active user and a session directly in the database and prints the cookie — never a real Google sign-in, and never a route. Specs are excluded from a bare `uv run pytest` / `make test`; run them explicitly with `make e2e`.

## Running locally without Docker

```bash
make install
cp .env.example .env      # then point DATABASE_URL at a Postgres you have running
make css                  # compiles static/css/app.css — Docker does this at image build time; a local run needs it too
uv run uvicorn ai_trainer.main:app_factory --factory --reload
```

Integration tests need a real PostgreSQL with pgvector reachable at `DATABASE_URL` — the simplest way is `make up` (or just `docker compose up -d db`) and let the app connect to `localhost:5432`. Then `make migrate` (or `uv run alembic upgrade head`) before running integration tests directly with `uv run pytest`; `make test`/`make test-integration`/`make coverage` already do this for you.

## Project structure

```
.
├── CLAUDE.md               # agent instructions: workflow, stack, non-negotiables
├── Dockerfile, compose.yaml, Makefile
├── docs/
│   ├── prd/                # product requirements (what & why) — one Approved at a time
│   ├── adr/                # architecture decisions (how) — stack, testing, workflow
│   └── templates/          # templates for adding a PRD/ADR/ticket
├── src/ai_trainer/         # hexagonal architecture (ADR-0003):
│   ├── domain/              #   entities, value objects, sport plugins — stdlib + Pydantic only
│   ├── application/         #   use cases; ports as typing.Protocol in application/ports/
│   ├── adapters/            #   concrete implementations of ports (DB, external services, …)
│   ├── llm/                 #   router, specialists, prompt templates (ADR-0007/0008)
│   ├── mcp/                 #   FastMCP server and tools (ADR-0010)
│   ├── knowledge/           #   retrieval and ingestion (ADR-0011)
│   ├── web/                 #   FastAPI routes, templates, static files (ADR-0012)
│   └── main.py               #   composition root — the only place concrete classes are wired
├── tests/
│   ├── unit/                # domain + application, with fakes
│   ├── integration/         # real PostgreSQL, adapters, routes, MCP
│   └── e2e/                 # pytest-playwright against the running stack
└── .claude/                 # agent rules and skills that automate the workflow above
```

`src/ai_trainer/` is the authoritative layout in [ADR-0003](docs/adr/0003-application-architecture.md); this tree is a convenience snapshot and may lag it slightly.

## How work happens

Tickets are GitHub Issues, implemented one at a time through a plan gate, a hands-on acceptance gate on the `make start` stack and a review gate, on their own branch, merged to `main` only via a human-reviewed PR. See [CLAUDE.md](CLAUDE.md) for the full loop.
