# Agent rules by topic

Rules load automatically when editing files that match their path scope. Read the relevant rule before working in that area.

| Rule | Scope | Governs |
|---|---|---|
| [context7.md](context7.md) | *Content-based* | **When:** user asks about a library, framework, SDK, API, CLI or cloud service (React, Prisma, FastAPI, Tailwind, etc.). **How:** Use Context7 MCP to fetch current docs instead of training data. Fetch one concept per query. Do not use for: refactoring, business logic, code review. |
| [python.md](python.md) | `src/**/*.py` | Layering (hexagonal), strict typing, Pydantic validation at boundaries, user scoping, time port, external source ports, SOLID principles. Never `datetime.now()` outside Clock; never prompt text outside tests. |
| [llm.md](llm.md) | `src/ai_trainer/llm/**` `src/ai_trainer/mcp/**` `src/ai_trainer/knowledge/**` `evals/**` | Prompts as files only (TemplateStr), model IDs from Settings, eval runs with `/tune-prompt`, an `llm_calls` row per call plus content-free OTLP traces, safety-first (wellbeing_or_injury), dates carry timezone, clarifications as choice cards, external data cached with source attribution. |
| [tests.md](tests.md) | `tests/**/*.py` | TDD (red → green → refactor), unit/integration/e2e split, no network (models.ALLOW_MODEL_REQUESTS = False), frozen time, fakes over mocks, tenancy tests, MCP tools via FastMCP Client(), pytest-asyncio auto mode. |
| [web.md](web.md) | `src/ai_trainer/web/**` | htmx 4 (not 2), partials for HX-Request, daisyUI 5 + Tailwind, SSE streams, user strings in templates, CSRF on POST, AI changes as confirm/edit/discard cards, ambiguity as choice card with option IDs. |
| [migrations.md](migrations.md) | `migrations/**` | Alembic autogenerate + review, never edit applied migrations, new user tables get user_id + CASCADE delete + index, vectors use HNSW index, up/down both work. |
| [docs.md](docs.md) | `docs/`, `CLAUDE.md`, `.claude/` | PRD owns *what*, ADR owns *how*. Exact one Approved PRD at a time. Only humans set Approved/Accepted. Stack decisions lock rules/skills. File numbers never change. Supersede old docs with links. |

## Quick lookup

- **"I'm editing Python code"** → read [python.md](python.md)
- **"I'm writing tests"** → read [tests.md](tests.md)
- **"I'm building an LLM feature"** → read [llm.md](llm.md)
- **"I'm adding a web page or form"** → read [web.md](web.md)
- **"I'm writing a database migration"** → read [migrations.md](migrations.md)
- **"I need a library API"** → read [context7.md](context7.md)
- **"I'm creating or updating docs"** → read [docs.md](docs.md)

## Related

- **Skills** (how to execute structured tasks): [../skills/README.md](../skills/README.md)
- **Main instructions**: [../../CLAUDE.md](../../CLAUDE.md)
- **Architecture & decisions**: [../../docs/adr/README.md](../../docs/adr/README.md)
