# Research

Cited findings that tickets and ADRs build on (ADR-0001, amended 2026-10-31). A research document informs and decides nothing: the PRD owns what, ADRs own how. A finding that should change a decision is proposed through `/update-docs`.

## Documents

| Document | Covers | Read it before |
|---|---|---|
| [mcp.md](mcp.md) | MCP tool design, server patterns, FastMCP and Pydantic AI versions | Building the MCP server or a tool (ADR-0010) |
| [knowledge-base.md](knowledge-base.md) | Ingestion, parsers, chunking, citation locators, evidence handling | Knowledge storage or ingestion (ADR-0011, ADR-0033) |
| [hybrid-search.md](hybrid-search.md) | Vector and full-text fusion, HNSW, English and Polish search, cheap retrieval checks | Search, or a better exercise shortlist (ADR-0031, ADR-0033) |

A link here may point at a document its research ticket hasn't written yet.

## Trusted sources

Cite only these, primary sources first:

1. **Specifications and official documentation:** the MCP specification, PostgreSQL's manual, a library's own docs (through context7, naming the version).
2. **Upstream code and release notes** of the project concerned.
3. **Engineering writing by the makers** of the technology, such as Anthropic on MCP and tool design, or pgvector's README on hybrid search.
4. **Peer-reviewed papers**, naming the venue, such as the reciprocal rank fusion paper.
5. **OpenRouter facts** from its MCP server, with the tool and the date (`.claude/rules/openrouter-mcp.md`).

Never cite SEO or tutorial blogs, vendor marketing, forum answers, unattributed benchmarks or an AI-written summary. They can point to a primary source, and the primary source is what gets cited.

## Rules

- Copy [the template](../templates/research.md). One document per topic, named as above.
- Every claim links its source, with a version or read date. A claim with no trusted source goes under Open questions, never in as fact.
- Keep to choices this project has to make. Check every recommendation against the ADRs and the `CLAUDE.md` non-negotiables.
- Reading is free: a research ticket makes no paid model call and sends no athlete data anywhere.
- Tickets that implement the area list the document in References, and `/apply-ticket` reads it while grounding the plan.
- When a later ticket proves a finding wrong, correct the document in that ticket's PR.
