---
name: context7-mcp
description: Metadata for the context7 rule — when to use Context7 MCP for library lookups instead of training data.
---

# Context7: Library documentation lookups

This is not a workflow skill to invoke. It documents when and how to apply the [context7 rule](../../.claude/rules/context7.md).

**When it applies:** Any time the user asks about a library, framework, SDK, API, CLI tool, or cloud service — even well-known ones like React, Prisma, FastAPI, Tailwind, or Django. Examples: setup questions, code generation, API references, configuration, version migration, debugging.

**How to execute:** Follow the steps in [.claude/rules/context7.md](../../.claude/rules/context7.md):
1. Resolve the library ID
2. Pick the best match
3. Query one concept per call
4. Answer from the fetched docs

**In this project:** Several pinned libraries are newer than training data (htmx 4, Pydantic AI v2, FastMCP 3). Use version-specific library IDs when a version is named or pinned in `CLAUDE.md`. Prefer official packages; cite library versions when they affect the answer.
