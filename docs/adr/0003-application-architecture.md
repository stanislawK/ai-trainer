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
