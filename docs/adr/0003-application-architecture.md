# ADR 0003 — Application architecture

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-16 |
| Related | PRD-0001 (G1, G4), ADR-0002, ADR-0006, ADR-0013 |

## Context

The app talks to several outside systems (web, database, LLM gateway, MCP clients, knowledge base) and must accept new sports as plugins (G4). TDD needs seams where fakes replace those systems. The project owner asked for SOLID design.

## Decision

Use a hexagonal (ports and adapters) architecture:

```
src/ai_trainer/
  domain/        # entities, value objects, sport plugins (domain/sports/<sport>/) — pure Python + Pydantic
  application/   # use cases; ports as typing.Protocol in application/ports/
  adapters/      # DB repositories, embedding client, clock, … — implement ports
  llm/           # router, specialists, prompt registry and templates (ADR-0007, ADR-0008)
  mcp/           # FastMCP server and tools (ADR-0010)
  knowledge/     # retrieval and ingestion (ADR-0011)
  web/           # FastAPI routes, templates, static files (ADR-0012)
  main.py        # composition root
```

Dependencies point inward: `domain` ← `application` ← `adapters`, `llm`, `mcp`, `knowledge`, `web`. Wiring happens only in the composition root, through FastAPI dependencies.

### Invariants

1. `domain` imports only the standard library and Pydantic — never `application`, `adapters`, `llm`, `mcp`, `knowledge` or `web`.
2. `application` depends on `domain` and on ports (`typing.Protocol`), never on a concrete adapter.
3. Concrete classes are created only in the composition root.
4. No `Any` in public signatures.
5. Every use case that touches user data takes the acting user's ID explicitly (ADR-0004, ADR-0005).
6. New behaviour arrives as new classes or plugins, not as growing `if` chains over types or sport names (open/closed).

## Consequences

- M0 adds an automated import-boundary check for invariants 1–2; the M0 ticket picks the tool and records it here.
- Tests use fakes that implement ports (ADR-0013).
- `.claude/rules/python.md` and the `add-endpoint`, `add-sport` and `add-mcp-tool` skills follow this layout.
- Tool chosen at #5: [import-linter](https://github.com/seddonym/import-linter), configured in `pyproject.toml` (`[tool.importlinter]`) with two contracts — pure-Python, config lives next to ruff/mypy. A `layers` contract maps invariant 2 and the internal half of invariant 1 directly onto the package layout (`domain` ← `application` ← the outer adapters/llm/mcp/knowledge/web group, siblings within that outer group left free to import each other since nothing in this ADR restricts that). A `forbidden` contract covers the rest of invariant 1 — that `domain` imports no third-party package but Pydantic — by naming every other current top-level dependency; a new dependency that `domain` should stay clear of needs adding to that list. Both contracts name this Decision's layers explicitly, so a new top-level package under `src/ai_trainer/` is itself a change to this ADR's Decision (`/update-docs`) — the same change should add it to the `layers` list. Run with `uv run lint-imports` / `make import-lint`; wired into CI (ADR-0013) and `make check`.
