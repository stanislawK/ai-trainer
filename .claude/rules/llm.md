---
paths:
  - "src/ai_trainer/llm/**"
  - "src/ai_trainer/mcp/**"
  - "src/ai_trainer/knowledge/**"
  - "evals/**"
---

# LLM, tools and knowledge rules (ADR-0007 – ADR-0011, ADR-0014 – ADR-0017)

- **Prompts:** only through the registry. Bodies are files at `src/ai_trainer/llm/prompts/<id>/v<N>.<locale>.md`, loaded as `TemplateStr`. Never prompt text in Python strings outside tests (ADR-0008, invariant 1) — test fixtures may hold literal text to script `FunctionModel` responses.
- **Versions:** never edit a template version that has a committed baseline; add `v<N+1>` and switch the registry.
- **Models:** model IDs and keys come from `Settings`, one model per template.
- **Evals:** a change to a template, a model ID or the router goes through `/tune-prompt` — eval run, no regression, new baseline in the same change. Set the judge model explicitly with `set_default_judge_model(OpenRouterModel(settings.eval_judge_model))`.
- **Logging:** record every LLM call with template ID, version, model, tokens, cost, latency and outcome.
- **Safety:** the `wellbeing_or_injury` intent is handled first (G3).
- **Dates:** every user-facing template's deps carry `today`, `now_local` and `timezone`. No date tool inside the app's own pipeline, and no wall-clock read outside the `Clock` adapter (ADR-0014).
- **Clarification:** a specialist that cannot proceed returns `Clarification` with concrete options, never a free-text question. The resumed run trusts option IDs, not label text (ADR-0015).
- **External sources:** route, crag and place data goes through its port, is cached with `source`, `source_url` and `fetched_at`, and is always shown with that attribution. Only a route, crag or place name leaves the app — never user identity, message text or profile data. Respect the per-user lookup budget; a miss or a low-confidence match becomes a clarification, never a guess (ADR-0016, ADR-0017).
- **MCP tools:** `user_id` only through `current_user_id(ctx)`, never a tool argument; meta from HTTP clients is ignored. Write tools create drafts or proposals only. Every tool returns `ToolResult[T]`; identity/tenancy failures raise, domain failures (bad input, empty result, spent lookup budget) return `success=False` with a `recovery_hint` (ADR-0010).
- **Knowledge:** answers cite their sources; web results are labeled as web; user data never enters the shared knowledge base.
