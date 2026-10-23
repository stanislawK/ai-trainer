# ai-trainer

An AI training companion for amateur athletes (climbing, gym, cycling). Document-driven, AI-first, TDD.

## Status

**M0 (Foundations) is complete; M1 (the clickable skeleton) is in progress.** So far: a typed Python/FastAPI skeleton, a `docker compose` stack (app + PostgreSQL/pgvector) with a `GET /health` endpoint that reports database reachability without needing a session, a styled base layout built with Jinja2 + htmx 4 + Tailwind CSS v4/daisyUI 5 (htmx is vendored under `static/`, the stylesheet is compiled at image build time with no Node.js anywhere), Google sign-in (`/auth/login`, `/auth/callback`, `/auth/logout`) with approval-gated accounts and a minimal admin user list at `/admin`, every other route gated on an active account, CSRF protection on non-GET requests (ADR-0005), account deletion, and the Liquid Glass `trainer-dark`/`trainer-light` themes and glass utilities (ADR-0019), dark by default and remembered per browser with no flash on reload.

The eval harness (`make evals` / `ai-trainer-evals`, ADR-0009) is wired up; the first dataset is the `router` template's (`make evals template=router`), and each M1 specialist template adds its own. See [CLAUDE.md](CLAUDE.md) for the current status, [docs/prd/README.md](docs/prd/README.md) and [docs/adr/README.md](docs/adr/README.md) for the product requirements and the architecture decisions that govern the stack.

## Continuous integration

Every push and pull request runs `ruff check`, `ruff format --check`, `mypy --strict`, the import-boundary check (`import-linter`, ADR-0003), squawk on the SQL of the branch's new migration revisions (ADR-0024), the OpenAPI snapshot drift check and the full `pytest` suite (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)), with integration tests hitting a real PostgreSQL/pgvector service container. Once that passes, a second `e2e` job builds and starts the full `docker compose` stack and runs the pytest-playwright specs in `tests/e2e/` (ADR-0013). Prompt evals never run here — they cost money and are triggered on demand, manually, through the separate `workflow_dispatch`-only [`.github/workflows/evals.yml`](.github/workflows/evals.yml) (ADR-0009).

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

Then set up Google sign-in (next section) — `Settings` refuses to start while any of its four keys is blank.

## Sign in with Google

The app signs in with Google only (ADR-0005). Every key below is required: `make start`, the test suite and every other command that builds `Settings` stop with an error naming the blank one (for example `GOOGLE_CLIENT_ID must not be blank`).

1. In [Google Cloud Console](https://console.cloud.google.com/), open **APIs & Services → OAuth consent screen**, and configure it: user type **External**, an app name and your email. While the app is in **Testing**, only the accounts listed under **Test users** can sign in — add every Google account you will use, admin and second account alike.
2. Open **APIs & Services → Credentials → Create credentials → OAuth client ID**, type **Web application**, with the authorized redirect URI `http://localhost:8000/auth/callback`.
3. Fill `.env`:
   - `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` from the client you just created;
   - `SESSION_SECRET_KEY` and `CSRF_SECRET_KEY` — two different values, each from `openssl rand -hex 32`;
   - `ADMIN_EMAILS` — your own Google email (comma-separated for more than one). A verified email in this list is `active` and an admin;
   - `PUBLIC_BASE_URL` — `http://localhost:8000` (already in `.env.example`; an origin with no path). A `.env` copied before #216 lacks it, and the app refuses to start naming the key.
4. Start the stack with `make start`, or restart it if it is already running: **`.env` is read only at startup**, so an edit does nothing until you restart.
5. Open **`http://localhost:8000`** — `localhost`, not `127.0.0.1`. Google matches the redirect URI exactly and the session cookie belongs to one host, so `127.0.0.1` fails with `redirect_uri_mismatch` or a state error — and the app answers any Host other than `PUBLIC_BASE_URL`'s with `400`. Click *Continue with Google*.

**Behind a proxy (ADR-0023).** The redirect URI sent to Google is always `PUBLIC_BASE_URL` plus `/auth/callback`, never built from the request, and only `PUBLIC_BASE_URL`'s host is accepted in the `Host` header. In production set it to `https://<your domain>` and register that `/auth/callback` on its own Google OAuth client. The image runs Uvicorn with `--proxy-headers`, which trusts `X-Forwarded-*` only from the addresses in the `FORWARDED_ALLOW_IPS` environment variable (default `127.0.0.1`): set it to the proxy's address or network, never `*`. The container health check (`scripts/healthcheck.py`) sends the `PUBLIC_BASE_URL` host itself, so it keeps passing.

Every sign-in re-checks `ADMIN_EMAILS` (ADR-0005): a listed, verified email always ends `active`, even if the account was `pending` or `disabled`. A second Google account (not listed) lands `pending`; an admin activates it at `http://localhost:8000/admin`. To lock an admin out for good, remove their email from `ADMIN_EMAILS` and restart — disabling a listed admin lasts only until their next sign-in.

If sign-in bounces back to the sign-in page with an error, `make logs` shows why (`google sign-in failed: …`); the log never contains tokens or claims. Safari and the `Secure` session cookie: see ADR-0005's Consequences.

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
| `make smoke-setup` / `make smoke-up` / `make smoke-down` | Run the production stack (`compose.prod.yaml`) locally behind Caddy — see [Production smoke run](#production-smoke-run) |
| `make cloud-init-check` | Validate the production host's `deploy/cloud-init.yaml` with Debian 13's cloud-init, in a container — see [deploy/RUNBOOK.md](deploy/RUNBOOK.md) |
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
| `make squawk` | Lint the SQL of migration revisions new since `origin/main` with squawk (ADR-0024); `base=<ref>` overrides the base |
| `make openapi` | Regenerate the OpenAPI snapshot (`docs/api/openapi.json`) |
| `make openapi-check` | Regenerate, then fail if the committed snapshot drifted (what CI runs) |
| `make check` | Everything CI runs: lint, format check, typecheck, import-lint, squawk, openapi-check, cloud-init-check, tests |
| `make purge-conversations` | Hard-delete conversations (and their messages) last active more than `CONVERSATION_RETENTION_DAYS` ago and print how many (ADR-0021); safe to repeat |
| `make llm-report [since=<ISO time>]` | Summarize the LLM debug log per template and model — see [Debugging LLM calls](#debugging-llm-calls) |
| `make latency-scenario [url=<base URL>]` | Replay the latency scenario on the running app and print p50/p95 per log turn — calls real models, costs money; see [Debugging LLM calls](#debugging-llm-calls) |
| `make evals template=<id>` | Run a prompt template's or a Jev decision set's eval dataset (ADR-0009, ADR-0020) — costs money, no default CI job runs it |

`test`, `test-integration`, `coverage` and `e2e` run `make migrate` first, so the `vector` extension always exists before the suite runs. `make evals` takes an optional `version=` and `model=` to override the template version or the model under test, and `repeat=N` to run every case N times (the baseline averages over all runs, so one noisy reply doesn't decide it); it needs `EVAL_JUDGE_MODEL` set in `.env` (ADR-0009) and a dataset at `evals/datasets/<id>.yaml`. The router's model comes from the required `ROUTER_MODEL` and the `chitchat` specialist's from the required `CHITCHAT_MODEL`, and every sport's extraction template's (`climbing.extract`, …) from the required `EXTRACT_MODEL`; each must differ from `EVAL_JUDGE_MODEL`. `ROUTER_PROVIDER_PREFERENCES` is a JSON object telling OpenRouter how to pick the router's upstream (`sort`, `preferred_max_latency`, `quantizations`, `only`; fallbacks stay on and `order` is refused), in the app and in `make evals` alike, and `EXTRACT_PROVIDER_PREFERENCES` does the same for the extraction templates; a `model=` override runs without them. `ROUTER_CALL_TIMEOUT_SECONDS` and `EXTRACT_CALL_TIMEOUT_SECONDS` bound each template's call, falling back to `LLM_CALL_TIMEOUT_SECONDS` (ADR-0026). The old single-slug `ROUTER_PROVIDER` and `EXTRACT_PROVIDER` are gone: a `.env` that still sets one fails at startup, so rename it. `CHAT_HISTORY_TURNS` sets how many recent messages both see. An `<id>` with a folder under `src/ai_trainer/llm/decisions/` (e.g. `make evals template=safety_screen repeat=3`) is a decision set: it runs on `DECISION_MODEL` with no judge, grades labels at their thresholds from `.env` (`SAFETY_SCREEN_PAIN_THRESHOLD`), and prints each case's probability spread over the repeats.

`CONVERSATION_RETENTION_DAYS` (default 14, at least 1) is how long a conversation lives after its last message (PRD-0004 B26). Past that it is hidden at once — not listed, not openable (404) — and `make purge-conversations` (`uv run ai-trainer purge-conversations`) deletes it for good, messages included, printing only the count. It never touches goals, profile rows or saved training sessions. Production runs it daily from the host scheduler (ADR-0023).

`SESSION_MERGE_WINDOW_MIN` (default 30, at least 1) is how close after the latest log of a saved session a new log in the same sport must come to be offered as an addition to it, "Add to the session at HH:MM" on its draft card (PRD-0004 B16, ADR-0014).

`LLM_DEBUG_LOG=true` (default off) logs every model request to `.llm-debug/` — see [Debugging LLM calls](#debugging-llm-calls).

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
- `make start` first runs `migrate` once in the foreground, so its output — including a blank or missing `Settings` key such as `GOOGLE_CLIENT_ID must not be blank` — shows in your terminal and a failure stops `make start` before anything else starts (`docker compose watch` streams only `app`).
- A one-shot `migrate` service (#64) runs `alembic upgrade head` against Postgres before `app` starts, on every `make start` — a fresh, empty volume reaches head, and a migration pulled since your last `make start` is applied on the next one; an up-to-date schema makes it a fast no-op. A failing migration keeps `app` from starting at all; its traceback shows in `make logs`.
- `make up`, the production image and CI's e2e job never use `--reload`, the CSS watcher, the migrate service or `compose.dev.yaml` — this is purely a local dev loop.

## Debugging LLM calls

Two records exist for every model call, and they answer different questions:

- **`llm_calls`** (Postgres, always on): one row per gateway call — template, version, model, total tokens, cost, latency and outcome (`success`, `timeout`, `error`, `cancelled`). It's the accounting record (ADR-0018): what a call cost and how it ended.
- **`.llm-debug/<UTC date>.jsonl`** (only with `LLM_DEBUG_LOG=true`): one JSON line per *HTTP request* inside a call — what was sent (`instructions`, `messages`, `output_schema`), what came back (`reply`), that request's own tokens, `cost` and `latency_ms`, and for streams `ttft_ms` (time to first text) and, if the stream was cut off, `partial_reply`. Lines of one call share a `call` id and number their `attempt`s; `error` names the exception when a request failed (`CancelledError` for a timeout or a closed stream).

Reach for them when a reply is slow or times out, when a template costs more than expected, when an extraction comes back wrong (to see exactly what the model was given), or when a call retried. The debug log holds athletes' words, so it stays on your machine and out of traces.

**Turn it on.** Set `LLM_DEBUG_LOG=true` in `.env` and restart `make start` (settings are read only at start). `make start` bind-mounts the container's `/app/.llm-debug` to `.llm-debug/` in the repo, git- and docker-ignored. Set it back to `false` when you're done; `rm .llm-debug/*.jsonl` clears old lines.

**Per-template report** — `make llm-report` (`uv run ai-trainer llm-debug-report`), optionally `since=2026-10-18T20:00Z` (`--since`; a time without a timezone is UTC, and a call counts when its first request is at or after it). It needs no database and runs on the host against `.llm-debug/`. The first table has one row per template version and model: calls, HTTP requests per call (above 1.00 means validation retries), p50/p95 call latency, p50 time to first text for streams, mean and max input and output tokens per call, total cost (`—` when the provider reports none, as for decision sets), and calls that ended in a timeout or an error. A call is its lines summed, as for its `llm_calls` row. `timeouts` counts calls whose last request logged `CancelledError`, so it also counts a closed stream, because the log can't tell the two apart. The second table gives the mean characters per request of each prompt part: `instructions`, `history` (the template's `## Recent messages` section plus earlier messages and retry prompts), `message` (the `## Latest message` section plus the athlete's user prompt, so a message sent in both places counts twice) and `output schema`. These are characters, not tokens (ADR-0018 invariant 6), and they show which part to shrink. Lines that aren't valid are skipped and counted in the footer, across the whole folder, `since` or not. With no log at all it prints `no debug log at <dir>` and exits 1.

**Latency scenario** — `make latency-scenario` (`uv run python -m scripts.latency_scenario`, optionally `url=` / `--url`, default `http://localhost:8000`) is the yardstick for log response time (PRD-0005 G14, ADR-0026). It **calls real models and costs money**. With `make start` running, it signs in as its own seeded climbing-only athlete (`dev-latency-scenario@example.com`), then sends ADR-0025's five messages and eight follow-up logs in one fresh conversation over HTTP, confirming each draft card except the 7a one. Per turn it prints the send-to-card time, outcome, USD (the sum of the turn's `llm_calls` rows; Jev rows carry no cost) and each call's template, latency and tokens. The last line gives p50 and p95 over log turns (nearest rank, failed turns included), timeouts and failures, and every turn that timed out or failed is named. The same summary goes to `.llm-debug/latency-scenario-<UTC time>.json`. With the stack down it exits 1 before seeding or sending anything. It needs no `LLM_DEBUG_LOG`; turn that on as well to follow a run up with `make llm-report since=…`.

**Per-call summary** — one object per gateway call, oldest first, with its requests summed:

```bash
jq -s 'group_by(.call) | sort_by(.[0].at) | map({template: .[0].template_id, v: .[0].template_version, attempts: length, input: (map(.input_tokens)|add), output: (map(.output_tokens)|add), cost: (map(.cost // 0)|add), latency_ms: (map(.latency_ms)|add), errors: (map(.error)|unique)})' .llm-debug/*.jsonl
```

`attempts` above 1 means an output-validation retry (each one is a billed request); `errors` lists what failed, `[null]` when nothing did. Use it to spot which step of a message was slow or expensive.

**The matching `llm_calls` rows** — the last hour, oldest first, so they line up with the summary above:

```bash
docker compose exec -T db psql -U ai_trainer -d ai_trainer -c "select template_id, template_version, input_tokens, output_tokens, cost, latency_ms, outcome from llm_calls where created_at > now() - interval '1 hour' order by created_at;"
```

For a `success` row, the summary's tokens and cost equal the row's, and its `latency_ms` is at most the row's (the row also times setup and validation). Decision sets (`safety_screen`, `router_fast`) show `cost: 0` in the summary and `NULL` in the row: their provider reports no cost. A `timeout` or `error` row records 0 tokens and no cost, while the summary still shows what its completed requests billed — that difference is spend the row doesn't count.

**Everything about one call** — after picking a `call` id from the raw lines (`jq '{call, template_id, attempt, error}' .llm-debug/*.jsonl`):

```bash
jq 'select(.call == "<call id>")' .llm-debug/*.jsonl
```

Shows the full instructions, messages, output schema and reply of each attempt — the place to read why an extraction came back wrong, or what the retry prompt told the model.

**Only the failures, retries or slow streams:**

```bash
jq 'select(.error != null) | {at, template_id, attempt, error, latency_ms, partial_reply}' .llm-debug/*.jsonl
jq 'select(.attempt > 1) | {at, template_id, call, attempt, error}' .llm-debug/*.jsonl
jq 'select(.streaming) | {at, template_id, ttft_ms, latency_ms}' .llm-debug/*.jsonl
```

The first finds requests that failed or were cut off by the timeout; the second, validation retries; the third, how long each streamed reply took to show its first text against its whole length.

## End-to-end tests

`tests/e2e/` (pytest-playwright, ADR-0013) runs against the full running app, not a test client, so it needs `make up` (or `make up-build`) first, and `make e2e-install` once to download the Chromium browser. Signed-in pages are reached through `scripts/dev_session.py`, a dev-only script that seeds an active user and a session directly in the database and prints the cookie — never a real Google sign-in, and never a route. It seeds an onboarded user; `uv run python scripts/dev_session.py --not-onboarded` seeds a separate fresh user (reset on every run) who lands on the onboarding sports step. Specs are excluded from a bare `uv run pytest` / `make test`; run them explicitly with `make e2e`.

## Production smoke run

`compose.prod.yaml` is the production stack (ADR-0023): Postgres, a one-shot `migrate`, the app and Caddy. Only Caddy publishes a port, 443, and it accepts only clients that present a certificate from the Authenticated Origin Pulls CA — in production, Cloudflare. To try it on your machine with test certificates:

```bash
make smoke-setup   # writes .smoke/: test CAs and certificates, a random DB password, an app .env from yours
make smoke-up      # builds ai-trainer:smoke, then starts the stack and waits until it's healthy
cd .smoke
curl -i --cacert origin-ca.pem --cert client.pem --key client-key.pem https://localhost/health
#   HTTP/2 200, strict-transport-security: max-age=31536000, {"database":"ok"}
curl --cacert origin-ca.pem https://localhost/health
#   fails the TLS handshake: curl exits 56 ("certificate required") on TLS 1.3, 35 on TLS 1.2
curl --cacert origin-ca.pem --cert rogue-client.pem --key rogue-client-key.pem https://localhost/health
#   fails too ("unknown ca"): only the AOP CA's certificates count
curl -s -D - -o /dev/null --cacert origin-ca.pem --cert client.pem --key client-key.pem https://localhost/auth/login
#   302 to Google with redirect_uri=https%3A%2F%2Flocalhost%2Fauth%2Fcallback
cd .. && make smoke-down
```

- It needs port 443 free and a filled-in `.env` (the Google keys may be dummies; only the sign-in redirect is checked). `make start` can keep running alongside: the smoke stack is its own Compose project, `ai-trainer-prod`, and publishes nothing else.
- In production `APP_IMAGE` is `ghcr.io/stanislawk/ai-trainer@sha256:<digest>`, `SITE_HOST` the host of `PUBLIC_BASE_URL`, and the config directory (`.env`, `db.env`, `certs/`) is `/opt/ai-trainer`. The smoke run uses a local tag instead, because a local build has no registry digest.
- `make smoke-down` keeps the smoke stack's volumes; `docker compose -p ai-trainer-prod down -v` drops them.

## Production host

The owner provisions the Hetzner host by following [deploy/RUNBOOK.md](deploy/RUNBOOK.md): stock and price check, Cloud Firewall, the server from [deploy/cloud-init.yaml](deploy/cloud-init.yaml), Cloudflare (Full strict, Origin CA, zone-level AOP) and the server's env files (ADR-0023). Agents never reach the host. `make cloud-init-check` validates the user data before it's used.

## Releasing

Only the owner releases (ADR-0023). Agents cannot tag: `.claude/hooks/guard_release.py` blocks it. A `v*` tag runs [`.github/workflows/release.yml`](.github/workflows/release.yml). Its `build` job pushes `ghcr.io/stanislawk/ai-trainer:<tag>`, fails the run if the image holds an env file or a private key, then attests the image's provenance. The image digest is the job's `digest` output and appears in the run log.

The `release` job then waits for the owner's approval of the `production` environment (Actions → the run → **Review deployments** → `production` → **Approve and deploy**). Once approved, it creates a GitHub Deployment for `production` whose payload is `{"digest": "sha256:…"}`, with status `queued`, which the server's deploy poller picks up. A rejected approval creates no deployment.

```bash
git tag v0.1.0 && git push origin v0.1.0      # owner only; any other tag runs nothing
gh run watch                                  # pick the "Release" run; approve when it waits at `release`
gh api repos/stanislawK/ai-trainer/deployments --jq '.[0] | {id, ref, environment, payload}'   # owner only
gh api repos/stanislawK/ai-trainer/deployments/<id>/statuses --jq '.[0].state'                 # queued
gh attestation verify oci://ghcr.io/stanislawk/ai-trainer@sha256:<digest> -R stanislawK/ai-trainer \
  --signer-workflow stanislawK/ai-trainer/.github/workflows/release.yml
docker logout ghcr.io; docker pull --platform linux/amd64 ghcr.io/stanislawk/ai-trainer@sha256:<digest>   # public: no login; amd64 only
```

The first release creates the GHCR package as private, so its visibility is set to public once by hand ([deploy/github-hardening.md](deploy/github-hardening.md), section 4).

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
