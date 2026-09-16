---
name: context7-mcp
description: This skill should be used when the user asks about libraries, frameworks, API references, or needs code examples. Activates for setup questions, code generation involving libraries, or mentions of specific frameworks like React, Vue, Next.js, Prisma, Supabase, etc.
---

When the user asks about libraries, frameworks, or needs code examples, use Context7 to fetch current documentation instead of relying on training data.

## When to Use This Skill

Activate this skill when the user:

- Asks setup or configuration questions ("How do I configure Next.js middleware?")
- Requests code involving libraries ("Write a Prisma query for...")
- Needs API references ("What are the Supabase auth methods?")
- Mentions specific frameworks (React, Vue, Svelte, Express, Tailwind, etc.)

## How to fetch documentation

The steps live in one place: `.claude/rules/context7.md` — resolve the library ID, pick the best match, then query one concept at a time. Follow them there rather than repeating them here, so the workflow has a single source of truth.

## In this project

- Use a version-specific library ID when the user names a version, or when this project pins one. Several pinned libraries are newer than most training data: htmx 4, Pydantic AI v2, FastMCP 3 (see `CLAUDE.md`).
- Prefer official or primary packages over community forks.
- Cite the library version when it affects the answer.
