# ai-trainer

An AI training companion for amateur athletes (climbing, gym, cycling). Document-driven, AI-first, TDD.

## Before any work

1. Read `docs/prd/README.md` and the current PRD it links.
2. Read `docs/adr/README.md` and every ADR relevant to the task.
3. Follow the matching `.claude/rules/` (they load automatically for matching paths) and skills (see guides below).

Docs outrank chat. If a request contradicts an Accepted ADR or the Approved PRD, or needs a decision no doc records, stop and propose a doc change with `/update-docs`. Never leave a product or stack decision only in chat.

## Guides & configuration

- [**.claude/rules/README.md**](.claude/rules/README.md) — Rules by topic; loaded automatically when editing files in their scope (Python, tests, web, LLM, migrations, etc.)
- [**.claude/skills/README.md**](.claude/skills/README.md) — Structured workflows for docs, tickets, evals, routes, tools, sports and design (invoke with `/skill-name`)
- [**docs/design/README.md**](docs/design/README.md) — Claude Design projects, the screen → template → ticket map, and the design brief

## How work happens

- Tickets are GitHub Issues in `stanislawK/ai-trainer`. Create them with `/create-tickets <scope>`; implement one with `/apply-ticket <issue#>`.
- Three human gates:
  - the **plan gate**: no product code before Approve;
  - the **acceptance gate**: a human tries the change on the running `make start` stack, following the agent's step-by-step script and opening any screenshots under `.acceptance/<issue#>/`, before the diff is reviewed;
  - the **review gate**: no commit, push or PR before Approve.
- Every change goes on its own branch and reaches `main` only through a PR a human merges. Ticket branches are `<area>/<issue#>-<slug>`; doc-only work without an issue uses `docs/<slug>`. Never commit to, push to or merge into `main` (ADR-0001).
- After opening a PR, the agent suggests the best next ticket without being asked (ready, unblocking, smallest); it never starts it.
- Prompt, model or router changes go through `/tune-prompt <template-id>`.
- Only a human sets a PRD to Approved or an ADR to Accepted.

## Stack

Locked by the ADRs cited below; `docs/adr/README.md` carries each ADR's current status. Changing any of it means changing its ADR through `/update-docs` — never a decision in chat.

- Python 3.14, uv, ruff, mypy `--strict`, pydantic-settings — ADR-0002
- Hexagonal architecture in `src/ai_trainer/` — ADR-0003
- PostgreSQL 18 + pgvector, SQLAlchemy 2 async + psycopg 3, Alembic — ADR-0004
- Google OAuth (Authlib) with server-side sessions — ADR-0005
- Sport plugins + `SportRegistry` — ADR-0006
- OpenRouter through Pydantic AI v2 — ADR-0007
- Router → specialists; prompt templates as `TemplateStr` files — ADR-0008
- Pydantic Evals — ADR-0009
- FastMCP 3 tools — ADR-0010
- Knowledge base on pgvector with hybrid search — ADR-0011
- FastAPI + Jinja2 + htmx 4 + daisyUI 5 — ADR-0012
- pytest TDD, 100% coverage on touched modules — ADR-0013
- `Clock` port, user timezone, session continuity — ADR-0014
- Structured clarification and the choice card — ADR-0015
- External route and crag reference data, grade conversion — ADR-0016
- Geographic lookup, provider deferred to an M5 spike — ADR-0017
- `llm_calls` accounting in Postgres; OpenTelemetry over OTLP, backend from settings — ADR-0018
- Liquid Glass daisyUI themes (`trainer-dark` default), glass utilities, Claude Design handoff and Playwright parity; new elements use Liquid Glass v4 (`mat-content` content, `lg-*` controls, no glass on glass) — ADR-0019
- Jev decision model (`typesafe/jev-1.13` on OpenRouter, via Pydantic AI `TypeSafeModel`) behind `DecisionGatewayPort`, the default for any step that only picks (classify, route, gate, score, a closed-set draft field) — ADR-0020
- Conversations, prompt history per conversation, 14-day retention with a purge CLI — ADR-0021
- Training-session tables: `training_sessions`, `training_session_sources`, `session_drafts`, `clarifications` — ADR-0022
- Hetzner + Caddy behind Cloudflare (Full strict, AOP); `v*` tag → attested GHCR image → owner-approved `production` environment → pull-based deploy; age-encrypted backups to R2 — ADR-0023
- Expand/contract, forward-only migrations gated by squawk and an upgrade-with-data test — ADR-0024
- LLM latency plan: model, upstream routing, reasoning effort and timeouts per template from `Settings`, picked on measured latency, cost and evals; follow-up logs routed by the conversation's sport — ADR-0026
- Per-athlete venues with their own gym grading, never converted without the athlete's mapping; opt-in device location matched against a bulk-imported OpenStreetMap gym directory — ADR-0027
- Athlete profile (`athlete_profiles`, `sport_profiles`) built by AI onboarding: one prompt template per step read in the background, durable `onboarding_answers`, a summary that catches up and is the only write — ADR-0028
- In-process request and model-call limits: sign-in routes by `CF-Connecting-IP`, other requests by session cookie, text and decision calls sharing one per-user budget; no Redis — ADR-0029

## Commands (available once M0 lands)

```
uv sync                          # install
uv run pytest                    # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy                      # strict type check
uv run lint-imports              # layer-boundary check (ADR-0003)
make squawk                      # squawk on the SQL of migration revisions new since origin/main (ADR-0024)
uv run python scripts/generate_openapi.py   # regenerate docs/api/openapi.json (never hand-edit it)
docker compose up -d             # app + postgres
make start                       # dev loop: app + postgres with uvicorn --reload and a live Tailwind CSS rebuild over src/ (foreground)
make cloud-init-check             # validate deploy/cloud-init.yaml with Debian 13's cloud-init (deploy/RUNBOOK.md provisions the host; owner only)
make backup-check                # round-trip deploy/backup.sh and the restore drill against the `make start` database with a throwaway age key (ADR-0023, #223, #224)
make smoke-setup && make smoke-up   # compose.prod.yaml locally behind Caddy with test certificates (README "Production smoke run"); `make smoke-down` stops it
git tag v0.1.0 && git push origin v0.1.0   # release, owner only (ADR-0023): starts release.yml; agents never tag (README "Releasing")
gh run watch                     # release, owner only: the run waits at `release`; approve in Actions → Review deployments → production
gh api repos/stanislawK/ai-trainer/deployments   # release, owner only: the approved run's deployment, payload = digest, status queued
uv run alembic upgrade head      # migrations
uv run ai-trainer purge-conversations   # delete conversations past CONVERSATION_RETENTION_DAYS (ADR-0021)
uv run ai-trainer llm-debug-report [--since <ISO time>]   # summarize LLM_DEBUG_LOG lines per template (ADR-0018)
make latency-scenario            # replay the latency scenario on `make start`, p50/p95 per log turn (ADR-0026; costs money)
uv run playwright install chromium   # one-time browser download for e2e
uv run pytest tests/e2e          # e2e specs (needs `docker compose up`; excluded from `uv run pytest`)
uv run ai-trainer-evals run <template-id> [--version N] [--promote]   # evals at the app's version; only --promote writes the baseline (costs money — run on purpose)
```

## Non-negotiables

- `user_id` comes only from the authenticated session; every query is user-scoped.
- Access is approval-gated: new accounts are `pending` until an admin activates them, and admin rights come from `ADMIN_EMAILS` — never from the database. A sign-in with a listed, verified email always ends `active` (ADR-0005).
- No real LLM calls in pytest.
- No prompt text in Python string literals outside tests (ADR-0008).
- Secrets live only in `.env`.
- AI-made data changes are drafts the user confirms.
- The clock is a port: no `datetime.now()` outside its adapter, and stored time is UTC (ADR-0014).
- Ambiguity is a choice card with concrete options, never a free-text question (ADR-0015).
- External lookups are cached and attributed, and carry no user data — only a route, crag or place name (ADR-0016).
- Europe first: metric units and the European grade scales (French, Font, UIAA, Polish/Kurtyka) must work. Imperial units and American grades (V, YDS) are best effort, and a gap in them never blocks a ticket, a prompt promotion or a release. Default to metric when unsure (PRD 0008 G15).
- A gym grade is never converted on its own: a bouldering gym's "6" is not 6A. A device position is read only with the athlete's consent, used for one gym match, and never stored, logged, sent to a model or sent out (ADR-0027).
- Look up library APIs with context7 (`.claude/rules/context7.md`) before using them. Several libraries here are newer than most training data: htmx 4, Pydantic AI v2, FastMCP 3.
- Facts about OpenRouter models, upstreams, prices and live latency come from the OpenRouter MCP server (`.claude/rules/openrouter-mcp.md`), never from memory. Its paid tools run only when the owner asks, and never with athlete data.
- Only stable/GA OpenRouter model IDs are used outside `/tune-prompt` experiments — no preview or beta models in default `Settings`.
- Jev only picks, wherever that helps: it classifies, routes, gates, scores or fills a closed-set draft field the athlete confirms. It never writes text, open values or arithmetic. It runs through the decision gateway with a pinned version, question sets as files, thresholds from evals, and a fallback. Its state never carries user identity.
- UI is built from its Claude Design mock with `/implement-design`; custom CSS lives only in the daisyUI theme and the glass utilities (ADR-0019).
- Production data is never deleted by a change: migrations are expand/contract and forward-only, and a contract step is its own owner-approved ticket (ADR-0024).
- Only the owner releases: agents never create or push tags, approve a deployment, call deployment APIs, or connect to the production host (ADR-0023).

## Status

M0 is complete: #2–#17, the design foundation #36–#44 with its fixes #58–#59, and the `make start` dev loop #62–#64 are merged. PRD 0008 (v0.9: Europe first, metric units and European grades as the bar, G15) is Approved and supersedes PRD 0007 (v0.8: AI onboarding and the onboarding part of the athlete profile, M1o), which superseded PRD 0006 (v0.7: gyms and the location check, never approved on its own) and PRD 0005 (v0.6: the logging response-time bound G14), which superseded PRD 0004 (v0.5: conversations, retention, a hosted POC), which superseded PRD 0003 (v0.4, sport inference and the sport glyph on replies), PRD 0002 and PRD 0001. Accepted: 0001–0009, 0012–0016, 0018–0028. 0010, 0011 and 0017 stay Proposed until their milestones. The Claude Design design system and screens exist ([docs/design/README.md](docs/design/README.md)). The M1 skeleton decisions are recorded in ADR-0005, ADR-0006 and ADR-0008. Next: M1a Conversations (from #146), M1 Log it, M1o Onboard me and M1z Ship it. The first production deploy comes after M1a, M1 and M1o; from then on, production data is never deleted (ADR-0024).
