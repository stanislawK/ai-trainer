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
- **Verified at #187 (2026-10-19):** Pydantic AI has no first-class `session_id` setting for OpenRouter. OpenRouter reads `session_id` as a top-level request-body field of at most 256 characters, and sticky routing starts from a session's first successful call ([prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching), `search-docs`). The gateway sends it through `extra_body`, which Pydantic AI 2.55 merges with the `provider` and `reasoning` blocks.
- **Step 3 result (#186, 2026-10-19).**
  - `ROUTER_REASONING_EFFORT` and `EXTRACT_REASONING_EFFORT` are in `Settings`, unset by default. When one is set, it is sent as `reasoning.effort`.
  - `get-model` (2026-10-19) lists glm-5.3-flash's efforts as `max`, `high` and `low`, with `max` as the default.
  - At `low` the evals regress, so `low` is not promoted and both settings stay unset (invariant 2). Figures from `ai-trainer-evals run <id> --repeat 3`, against the committed baselines:
    - `router`: 0.922 against 1.000 (one run at the default effort scored 0.979 the same day). Three cases fail every repeat: a question followed by a log comes back with its intents out of order, a prompt injection is routed `unclear` rather than `chitchat`, and a single-sport cyclist's "2 hours easy" is routed `request_plan`.
    - `climbing.extract`: 0.984 against 1.000. French sport grades fail every repeat.
    - `cycling.extract`: 0.943 against 0.977. A speed read as a distance, "on Tuesday" when today is Tuesday, and a Polish log fail every repeat.
    - `gym.extract`: 1.000 against 0.994, in one run.
  - The output-token cut at `low` was not measured, because no regressing setting is promoted. Step 2's model spike carries the latency cut. Its candidates set their own effort through these settings.
- **Step 4 result (#187, 2026-10-20).**
  - `ROUTER_PROVIDER_PREFERENCES` and `EXTRACT_PROVIDER_PREFERENCES` replace the single-slug `ROUTER_PROVIDER` and `EXTRACT_PROVIDER` pins and `pinned_upstream()`. Each is a JSON object with `sort`, `preferred_max_latency`, `quantizations` and `only`. Fallbacks stay on, and `order` is refused at startup. A `.env` that still sets an old key fails at startup. The eval CLI sends the same block, and a `--model` override drops it.
  - `ROUTER_CALL_TIMEOUT_SECONDS` and `EXTRACT_CALL_TIMEOUT_SECONDS` bound each template's call. Unset, they fall back to `LLM_CALL_TIMEOUT_SECONDS`.
  - Router and extraction calls send `session_id` = sha256 of `ai-trainer-session:<conversation id>`. It is stable within a conversation and different across conversations. It never contains a user ID or email (invariant 4), and it links back to a conversation only for someone holding the database.
  - Ordering: this step landed before the step 2 model spike (#188). The mechanism does not depend on the model; #188 re-tunes the values when it changes models.
  - Upstreams of `z-ai/glm-5.3-flash` (`list-model-endpoints`, 2026-10-19, 30-minute window):
    - the `z-ai` endpoint: p50 3.05 s, p90 9.9 s;
    - Modal (nvfp4): p50 0.34 s, p90 0.73 s;
    - Wafer (nvfp4): p50 0.39 s, p90 1.0 s;
    - BaseTen (fp8): p50 0.45 s, p90 1.9 s.
    - The quantization filter `["fp8","nvfp4"]` keeps the router off the mixed fp4 stacks that made its evals noisy (#127).
  - Measured with `make latency-scenario` on 2026-10-20, using `{"sort":"latency","quantizations":["fp8","nvfp4"]}` for both templates and timeouts unset, against ADR-0025's figures (19–43 s per log turn, median 35 s):
    - 12 log turns: p50 1.7 s, p95 21.1 s, no timeouts or failures.
    - USD per log turn: 0.000115–0.000712.
    - Turn 1 alone set the p95: its `climbing.extract` call took 20.4 s for 1036 output tokens, the session's first, cold call. Later extraction calls took 0.6–1.6 s, and the router took 1.2–1.6 s.
    - G14's p95 still fails on a session's first log; steps 2 and 5 carry that.
  - Timeouts in `.env.example`, from these measurements:
    - router: 5 s, about three times its 1.6 s p95;
    - extraction: 30 s, above its 20.4 s cold p95. A timeout near the warm figures would fail a session's first log.
  - Evals with these preferences (`ai-trainer-evals run <id>`, one run each, 2026-10-20):
    - `climbing.extract`: 0.984 against 1.000. That is a regression (invariant 2), the same figure #186 got at effort `low`.
    - `router`: ran at v1 rather than the active v2, so it is not comparable.
    - So `.env.example` recommends no provider preferences, the committed baselines stay as they were, and the code defaults stay unset. #188 picks the values on its own evals and the latency scenario.
- **Step 2 result (#188, 2026-10-20).** The owner kept `z-ai/glm-5.3-flash` at its default reasoning effort for the router and every extraction template, and promoted #187's routing, `{"sort":"latency","quantizations":["fp8","nvfp4"]}`, with timeouts of 5 s for the router and 30 s for extraction. `.env.example` carries those values, so the Step 4 result's "no provider preferences" recommendation no longer holds.
  - **Shortlist** (`get-model`, `list-model-endpoints`, 2026-10-20). At model level, every candidate lists tools, structured outputs and `reasoning_effort`.
    - `z-ai/glm-5.3-flash`: reasoning is mandatory (`max`, `high`, `low`; default `max`). It costs $0.048/$0.16 per M tokens on its cheapest upstream (fp4) and $0.15/$0.50 on the fast fp8 upstreams. Tested at default effort as the control, and at `low`.
    - `deepseek/deepseek-v4-flash-0731` at `none`: reasoning is optional. A dated snapshot, 81 days old rather than days old. Its fast fp8 upstreams cost $0.06–0.13/$0.18–0.28.
    - `openai/gpt-6-luna` at `none`: reasoning is optional. 28 days old, GA, and moderated.
      - It costs $0.10/$0.50 on the default OpenAI and Azure tiers. The regional Azure endpoints and Bedrock cost $0.11/$0.55, the priority tier $0.20/$1.00, and the flex tier $0.05/$0.25 (p50 9.5 s).
      - Every endpoint reports quantization `unknown`, so a `quantizations` filter matches none of them.
    - **Per upstream:** gpt-6-luna offers all three capabilities on every endpoint. glm and DeepSeek have gaps on upstreams these variants can reach:
      - BaseTen and CoreWeave report `tool_choice` `required: false`.
      - BaseTen's DeepSeek endpoint lists no `structured_outputs`.
      - Several fp8 hosts lack `structured_outputs`: Z.AI, SiliconFlow, AtlasCloud, GMICloud, Novita and StreamLake.
      - Preferences set no `require_parameters`, so OpenRouter may route to such an upstream. No eval or scenario error came from a missing capability.
  - **Method.**
    - Evals: `ai-trainer-evals run <id> --repeat 3`, with `router` at v2, compared with the committed baselines (router 1.000, climbing 1.000, cycling 0.977, gym 0.994).
    - Latency: `make latency-scenario` against a second app on :8001 with the cell's settings. Percentiles are the scenario's nearest rank. The athlete is climbing-only, so latency is measured on `climbing.extract` only.
    - Each cell was measured once with timeouts unset. The two final setups were measured four times each, with their timeouts set, and pooled over 48 log turns.
    - USD is per log turn, as min / median / mean / max. Per-run summaries are under `.llm-debug/latency-scenario-20261020T*.json`.

    | Model · effort · routing | router | climbing | cycling | gym | p50 | p95 | Timeouts / errors | USD per log turn |
    |---|---|---|---|---|---|---|---|---|
    | **glm-5.3-flash · default · fp8 + nvfp4 (#187's), 4 runs** | **0.972** | **0.995** | **0.983** | **1.000** | **2.0 s** | **9.4 s** | **none** | **0.000091 / 0.000103 / 0.000226 / 0.001722** |
    | glm-5.3-flash · default · fp8 | 1.000 | 0.984 | 0.983 | 1.000 | 11.8 s | 30.5 s | 1 timeout | mean 0.000814 |
    | glm-5.3-flash · default · only baseten, morph | 1.000 | 0.984 | 0.989 | 1.000 | 8.5 s | 52.3 s | 3 timeouts | mean 0.000692 |
    | glm-5.3-flash · low · fp8 | 1.000 | 0.984 | 0.971 | 1.000 | 3.8 s | 9.3 s | none | mean 0.000490 |
    | glm-5.3-flash · low · only baseten, morph | 1.000 | 0.984 | 0.971 | 0.987 | 2.7 s | 9.0 s | none | mean 0.000450 |
    | deepseek-v4-flash-0731 · none · fp8 | 0.950 | 0.979 | 0.966 | 0.981 | 3.1 s | 16.6 s | none | mean 0.000246 |
    | deepseek-v4-flash-0731 · none · only coreweave, deepinfra, baseten | 0.965† | 0.984† | 0.966† | 0.981† | 3.7 s | 7.8 s | 6 errored turns | mean 0.000097 |
    | gpt-6-luna · none · sort latency, 4 runs | 1.000 | 0.984 | 1.000 | 1.000 | 2.8 s | 10.5 s | none | 0.000129 / 0.000142 / 0.000210 / 0.000364 |
    | gpt-6-luna · none · only azure | 1.000 | 0.984 | 1.000 | 1.000 | 2.8 s | 14.1 s | none | mean 0.000210 |

    † Scored over the cases that completed. 18 cases errored on upstream 429 rate limits (BaseTen and DeepInfra's shared pool) and 504 provider timeouts, not on a missing capability. `llm_calls` records only `outcome: error` for the six scenario turns, so their cause is inferred from the eval errors.
  - **Dropped, with reasons:**
    - glm at default effort on fp8 alone: misses G14 by 3× and times out. That filter excludes the fast nvfp4 upstreams (Modal, Wafer), leaving slower fp8 hosts while extraction writes 340–1,480 reasoning tokens.
    - glm at `low`: regresses `cycling.extract` on both routings, and `gym.extract` on the `only` list. That repeats #186's finding, this time on clean routing.
    - DeepSeek on fp8: regresses the three extraction templates. Its router's 0.950 falls within the router's allowed drop of 0.05.
    - DeepSeek on its `only` list: errors, as above.
    - gpt-6-luna on `only: ["azure"]`: 14.1 s p95.
    - gpt-6-luna on `sort: latency` was the runner-up: the best evals, with router, cycling and gym at 1.000. Over four runs it missed G14's p95 by 0.5 s (single runs 6.0, 8.5, 10.9 and 12.9 s), and its median USD per log turn was 13% above #187's.
  - **The pick against G14:** pooled over 48 log turns, p50 is 2.0 s and p95 9.4 s, with no timeouts or failures, so G14 is met. Single runs varied: p95 3.2, 2.7, 12.7 and 22.8 s. The tail is spiky. Calls took:
    - router: p95 1.4 s, maximum 1.4 s;
    - `climbing.extract`: p95 7.8 s, maximum 22.5 s, from one slow call.
  - **Evals against the committed baselines:**
    - `router` 0.972 lies within its allowed drop of 0.05. "mixed log and plan" and "mixed question then log" fail some repeats, as they did with DeepSeek.
    - `climbing.extract` 0.995 is the stale case below, failing 1 of 3 repeats.
    - `cycling.extract` 0.983 and `gym.extract` 1.000 are above their baselines and are promoted.
    - `router` and `climbing.extract` keep their committed 1.000, so a future run is held to the higher figure.
  - **Cost:** against #187's figures for the same setup (0.000115 / 0.000126 / 0.000224 / 0.000712), the median USD per log turn fell to 0.000103. The mean was 0.000226, and the maximum rose to 0.001722 on the slow call. The owner approved the pick with these figures shown, including the higher maximum (invariant 6).
  - **Timeouts:** 5 s for the router and 30 s for extraction, #187's values, are kept above the measured maxima (1.4 s and 22.5 s). A timeout near p95 would fail a slow but normal answer, which G14 rules out. This refines step 4's "from the measured p95": the p95 informs the value and the maximum bounds it. No timeout fired in the four runs.
  - **Stale climbing case.** "follow-up: only the new activities, day from history" expects `date_source: relative`.
    - The template's rule gives `assumed_today` when the latest message names no day, and the draft card then shows "Assumed: today".
    - Every candidate failed it, and so did glm at default effort on `only: ["z-ai"]`, the upstream the 1.000 was recorded on: 0.984, failing 3 of 3 repeats. The case, the rule and the baseline date from #153, so model or upstream behaviour on an ambiguous rule has shifted since.
    - Whether a day carried over from an earlier turn counts as `relative` is settled in a follow-up through `/tune-prompt`, where the label or the prompt changes and the baseline is re-recorded.
  - **Eval CLI gotchas found here**, for a follow-up ticket:
    - `--version` defaults to 1, while the router's active version is 2.
    - A run saves its baseline even when it regresses or runs another version, because the file is keyed by template ID only.
    - The eval path applies no per-call timeout. One stuck upstream call held a whole router run for 26 minutes.
  - **Seen on gpt-6-luna:** Pydantic AI drops `temperature` for it even at effort `none`. Its judge, `EVAL_JUDGE_MODEL` (`openai/gpt-6-luna-pro`), is of the same family. Both matter only if gpt-6-luna is reconsidered.
  - **Integration tests** run against the dev database. `test_purge_deletes_only_expired_conversations_of_every_user_and_returns_the_count` counts every `chat_messages` row, so it fails once the scenario's athletes exist. That happens on `main` too.
- **Owner check:** a low OpenRouter credit balance adds latency ([latency guide](https://openrouter.ai/docs/guides/best-practices/latency-and-performance)).
- **Status:** ADR-0025 stays the Accepted baseline; the ADR index notes that its options were picked here.
