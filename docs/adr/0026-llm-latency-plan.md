# ADR 0026 — LLM latency plan

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-19 |
| Related | PRD-0005 (G14, B1, B16, B23), ADR-0007, ADR-0008, ADR-0009, ADR-0018, ADR-0020, ADR-0025 (picks from its options) |

## Context

A log takes 19–43 s from send to draft card, with a median of 35 s (ADR-0025). Follow-ups in one conversation slow down, and long chains sometimes time out. PRD-0005 G14 bounds a log turn at p50 ≤ 5 s and p95 ≤ 10 s. ADR-0025 left the options to the owner. This ADR records the pick, made 2026-10-19 after a second look at the traffic log and OpenRouter's live data.

- **Two slow calls in series.** A log runs `router` (6–23 s), then `climbing.extract` (5–24 s). Jev takes about 0.35 s.
- **Reasoning cannot be switched off on the current model.** On `z-ai/glm-5.3-flash` it is mandatory: the efforts are `max`, `high` and `low`, and the default is `max` (OpenRouter `get-model`). That is why each call spends 300–900 output tokens to produce about 100 characters of JSON.
- **The upstream pin is a second, independent cause.**
  - `pinned_upstream()` sends `provider.order=["z-ai"]` for router and extract.
  - Over 30 minutes on 2026-10-19, that upstream measured p50 5.2 s, p90 15.1 s and p99 47.5 s, at 36 tok/s.
  - Other upstreams of the same model measured p50 0.35–0.56 s and p90 0.78–1.7 s, at 83–178 tok/s, for the same or lower price.
  - An explicit `order` also "disables sticky-session reordering and load balancing" ([latency guide](https://openrouter.ai/docs/guides/best-practices/latency-and-performance)).
  - One extract call took 23.6 s for 88 output tokens.
- **Follow-ups slow down through reasoning, not input size.** Router input grows only from 2177 to 2346 tokens over a chain. The model deliberates over whether a follow-up continues, repeats or edits an earlier log, and the fixed reply "Check it before it's saved" in history misled it (ADR-0025 Consequences).
- **Extraction carries text it does not need.** It gets the persona and ten turns of history. The assistant turns there are fixed lines that hold no draft content.

## Decision

1. **Measure with one command.** The ADR-0025 scenario becomes a repeatable command. It runs against the running app as a seeded single-sport athlete and adds a chain of eight follow-up logs. It reports send-to-card time per turn, `llm-report` per template, USD per log turn and any timeouts. G14 is judged on its output.
2. **Pick the router and extraction models on measurements.**
   - Candidates are shortlisted from OpenRouter's catalogue, the Artificial Analysis benchmarks and the live endpoint stats, with the reason for every pick and every rejection recorded. Filters: stable/GA, structured outputs and tools, reasoning that is optional or can be lowered, price.
   - Each candidate is measured on latency, USD per log turn and the router and extraction evals.
   - First shortlist: `deepseek/deepseek-v4-flash-0731` and `openai/gpt-6-luna` (both accept reasoning `none`), plus `z-ai/glm-5.3-flash` at effort `low` on a fast upstream.
   - The results table is appended here.
3. **Set reasoning effort per template.** It lives in `Settings` and is sent as `OpenRouterModelSettings.openrouter_reasoning={"effort": …}`. Use `none` where the model supports it, otherwise the lowest supported level (`low` for glm-5.3-flash).
4. **Route upstreams by measured latency.**
   - Replace the hard `order` pin with provider preferences from `Settings` per template: `sort`, `preferred_max_latency`, `quantizations` or `only`, set from step 2's measurements.
   - Send a per-conversation `session_id`, so follow-ups stay on the upstream that holds their warm KV cache.
   - Per-template call timeouts come from the measured p95, so a stuck upstream fails fast.
5. **A follow-up log takes its sport from the conversation.** Three conditions must all hold: Jev is confident the message is a single `log_session`; the conversation's latest draft falls inside the merge window; and that draft's sport is still plausible. The log then goes to that sport's extraction, and the LLM router is skipped (B23 allows inference from "the current conversation"). Otherwise the router decides, as now.
6. **Templates carry only what their task needs.**
   - Templates that never write to the athlete (`router`, `<sport>.extract`) do without the persona.
   - Extraction sees the athlete's own recent messages only, never the fixed assistant lines.
   - Every template puts its dynamic parts (date, time, history, message) after the static instructions, so the static prefix can be cached.
7. **Flatten the router's output schema.** One intent object with a `kind` enum and an optional `sport` replaces the ten near-identical `$defs`.
8. **Investigate background replies.** Accepting a message instantly and preparing the draft outside the reply stream is a spike, not a decision. If G14 still fails after steps 2–7, it gets its own ADR.

### Invariants

1. A ticket that changes latency runs the step 1 command and reports p50 and p95 per log turn, USD per log turn and any timeouts in its PR, compared with ADR-0025's figures. It never edits those figures.
2. A change to a prompt, model, schema, reasoning effort or the router goes through `/tune-prompt`, and evals must not regress (ADR-0009). A Jev threshold changes only through evals (ADR-0020).
3. Model IDs, provider preferences, reasoning effort and timeouts come from `Settings` per template. No provider slug or effort level is hard-coded.
4. Nothing sent to OpenRouter for routing carries user identity: the `session_id` is an opaque per-conversation value, never a user ID or email (ADR-0018, ADR-0020).
5. A sport taken from the conversation must be among the plausible sports for the message. If it is not, the LLM router decides (B23).
6. Only the owner approves a change that meets G14 by raising USD per log turn.

## Alternatives considered

- **No sports set: let Jev pick among all sports** (ADR-0025 option 4). Onboarding requires at least one sport (`src/ai_trainer/application/onboarding.py`), so real athletes never hit this case.
- **Extract alongside the router on speculation** (ADR-0025 option 5). Deferred: steps 2–5 remove the router from most log turns first.
- **One merged route-and-extract call.** It ties routing to one sport's schema, which weakens multi-sport messages and the separate evals.
- **EU in-region routing** (`eu.openrouter.ai`). It requires the Business plan and fails closed. glm-5.3-flash has a single EU endpoint, so there is no fallback ([in-region routing](https://openrouter.ai/docs/guides/features/in-region-routing.md)). Network round trip is about 0.1 s against seconds of generation, so this is a data-residency decision, not a latency fix.
- **Cap or summarise history.** History is too small to matter for latency (ADR-0025). Step 6 trims what extraction sees for accuracy, not size.

## Consequences

- **Tickets** come from `/create-tickets`, one per Decision step. The measurement command (step 1) comes first. The routing change (step 4) is blocked by the model spike (step 2).
- **Agent tooling.** The OpenRouter MCP server (`https://mcp.openrouter.ai/mcp`) is declared in `.mcp.json`, and `.claude/rules/openrouter-mcp.md` tells agents when to use it: model and upstream selection, prices, benchmarks, live endpoint latency and throughput, uptime history and the cost of a generation. It is a build-time tool only; the app calls the OpenRouter API directly ([MCP server](https://openrouter.ai/docs/guides/overview/mcp-server)).
- **Verified 2026-10-19:**
  - Pydantic AI 2.55 (installed) passes `openrouter_reasoning` and `openrouter_provider` (including `sort`) into the request body.
  - OpenRouter lists `reasoning_effort` as supported on every glm-5.3-flash upstream.
- **Unverified, flagged:**
  - Pydantic AI has no first-class `session_id` setting for OpenRouter. The field name and its placement (`extra_body`) are checked against OpenRouter's API reference in the step 4 ticket.
- **Step 3 result (#186, 2026-10-19).**
  - `ROUTER_REASONING_EFFORT` and `EXTRACT_REASONING_EFFORT` are in `Settings`, unset by default. When one is set, it is sent as `reasoning.effort`.
  - `get-model` (2026-10-19) lists glm-5.3-flash's efforts as `max`, `high` and `low`, with `max` as the default.
  - At `low` the evals regress, so `low` is not promoted and both settings stay unset (invariant 2). Figures from `ai-trainer-evals run <id> --repeat 3`, against the committed baselines:
    - `router`: 0.922 against 1.000 (one run at the default effort scored 0.979 the same day). Three cases fail every repeat: a question followed by a log comes back with its intents out of order, a prompt injection is routed `unclear` rather than `chitchat`, and a single-sport cyclist's "2 hours easy" is routed `request_plan`.
    - `climbing.extract`: 0.984 against 1.000. French sport grades fail every repeat.
    - `cycling.extract`: 0.943 against 0.977. A speed read as a distance, "on Tuesday" when today is Tuesday, and a Polish log fail every repeat.
    - `gym.extract`: 1.000 against 0.994, in one run.
  - The output-token cut at `low` was not measured, because no regressing setting is promoted. Step 2's model spike carries the latency cut. Its candidates set their own effort through these settings.
- **Owner check:** a low OpenRouter credit balance adds latency ([latency guide](https://openrouter.ai/docs/guides/best-practices/latency-and-performance)).
- **Status:** ADR-0025 stays the Accepted baseline; the ADR index notes that its options were picked here.
