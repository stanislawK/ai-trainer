---
paths:
  - "src/ai_trainer/llm/**"
  - "src/ai_trainer/mcp/**"
  - "src/ai_trainer/knowledge/**"
  - "evals/**"
---

# LLM, tools and knowledge rules (ADR-0007 – ADR-0011)

- **Prompts:** only through the registry. Bodies are files at `src/ai_trainer/llm/prompts/<id>/v<N>.<locale>.md`, loaded as `TemplateStr`. Never prompt text in Python strings outside tests (ADR-0008, invariant 1) — test fixtures may hold literal text to script `FunctionModel` responses.
- **Versions:** never edit a template version that has a committed baseline; add `v<N+1>` and switch the registry.
- **Models:** model IDs and keys come from `Settings`, one model per template.
- **Evals:** a change to a template, a model ID or the router goes through `/tune-prompt` — eval run, no regression, new baseline in the same change. Set the judge model explicitly with `set_default_judge_model(OpenRouterModel(settings.eval_judge_model))`.
- **Logging:** record every LLM call with template ID, version, model, tokens, cost, latency and outcome.
- **Safety:** the `wellbeing_or_injury` intent is handled first (G3).
- **MCP tools:** `user_id` only through `current_user_id(ctx)`, never a tool argument; meta from HTTP clients is ignored. Write tools create drafts or proposals only.
- **Knowledge:** answers cite their sources; web results are labeled as web; user data never enters the shared knowledge base.
