# ai-trainer

An AI training companion for amateur athletes (climbing, gym, cycling). Document-driven, AI-first, TDD.

## Before any work

1. Read `docs/prd/README.md` and the current PRD it links.
2. Read `docs/adr/README.md` and every ADR relevant to the task.
3. Follow the matching `.claude/rules/` (they load automatically for matching paths) and skills (see guides below).

Docs outrank chat. If a request contradicts an Accepted ADR or the Approved PRD, or needs a decision no doc records, stop and propose a doc change with `/update-docs`. Never leave a product or stack decision only in chat.

## Guides & configuration

- [**.claude/rules/README.md**](.claude/rules/README.md) — Rules by topic; loaded automatically when editing files in their scope (Python, tests, web, LLM, migrations, etc.)
- [**.claude/skills/README.md**](.claude/skills/README.md) — Structured workflows for docs, tickets, evals, routes, tools, and sports (invoke with `/skill-name`)

## How work happens

- Tickets are GitHub Issues in `stanislawK/ai-trainer`. Create them with `/create-tickets <scope>`; implement one with `/apply-ticket <issue#>`.
- Two human gates: the **plan gate** (no product code before Approve) and the **review gate** (no commit, merge or issue close before Approve).
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

## Commands (available once M0 lands)

```
uv sync                          # install
uv run pytest                    # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy                      # strict type check
docker compose up -d             # app + postgres
uv run alembic upgrade head      # migrations
uv run ai-trainer-evals run <template-id>   # evals (costs money — run on purpose)
```

## Non-negotiables

- `user_id` comes only from the authenticated session; every query is user-scoped.
- Access is approval-gated: new accounts are `pending` until an admin activates them, and admin rights come from `ADMIN_EMAILS` — never from the database (ADR-0005).
- No real LLM calls in pytest.
- No prompt text in Python string literals outside tests (ADR-0008).
- Secrets live only in `.env`.
- AI-made data changes are drafts the user confirms.
- The clock is a port: no `datetime.now()` outside its adapter, and stored time is UTC (ADR-0014).
- Ambiguity is a choice card with concrete options, never a free-text question (ADR-0015).
- External lookups are cached and attributed, and carry no user data — only a route, crag or place name (ADR-0016).
- Look up library APIs with context7 (`.claude/rules/context7.md`) before using them. Several libraries here are newer than most training data: htmx 4, Pydantic AI v2, FastMCP 3.
- Only stable/GA OpenRouter model IDs are used outside `/tune-prompt` experiments — no preview or beta models in default `Settings`.

## Status

Bootstrap: docs only, no application code yet. PRD 0001 (v0.2) is Approved. Every ADR that M0 cites is Accepted (0001–0005, 0007–0009, 0012–0014, 0018); 0006, 0010, 0011 and 0015–0017 stay Proposed until their milestones. M0 is ticketed as issues #2–#17. Next: `/apply-ticket 2`.
