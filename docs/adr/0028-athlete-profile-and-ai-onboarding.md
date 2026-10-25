# ADR 0028 — Athlete profile and AI onboarding

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-24 |
| Related | PRD-0007, superseded by [PRD 0008](../prd/0008-ai-training-companion.md) (B9, B10, B15, B24, B33–B36, F6, F18–F21, G1, G7, G15), ADR-0004, ADR-0006, ADR-0008, ADR-0012, ADR-0014, ADR-0018, ADR-0019, ADR-0020, ADR-0024, ADR-0026, ADR-0027 |

## Context

PRD-0007 replaces the four onboarding forms with a sport pick, four short prose chats per sport, and a summary the athlete confirms (B33–B36, F20, F21). The design is `Onboarding v4.dc.html` in the Liquid Glass project ([docs/design/README.md](../design/README.md)).

Today's onboarding (`application/onboarding.py`, `web/onboarding.py`) makes no model calls. It stores sports (`user_sports`), weekly minutes (`weekly_availability`), goals (`goals`) and the timezone (`users.timezone`). The app has no background jobs: every model call runs inside its request, and only the chat reply streams, over SSE.

The owner requires that the athlete never wait between steps. Only the summary may wait.

## Decision

**Profile storage.** Two new user-owned tables, each with `user_id` (FK `users` ON DELETE CASCADE, so they join account deletion, G7) and an index. Both are expand-only migrations (ADR-0024).
- `athlete_profiles`: `user_id` as PK, `birth_year` smallint, `height_cm` smallint, `weight_kg` numeric(4,1), `limits` varchar(300). All are nullable. Age is stored as `birth_year`, derived from the stated age and the `Clock`'s current year (ADR-0014), so it doesn't go stale. The summary shows it as an age again.
- `sport_profiles`: PK `(user_id, sport_id)`, `level` varchar(200), `exercises` varchar(300), `sessions_per_week` smallint (1–14), `session_minutes` smallint (5–600), `preferred_days` smallint[] (0–6). All are nullable.
- Goals go to the existing `goals` table (`text`, `target_date`, `sport_id`), and sports to `user_sports`.
- `weekly_availability` is no longer written. The schedule is per sport now (B33). Dropping that table is a later contract ticket, approved by the owner (ADR-0024).

**Answers are durable; reading them is not.**
- A third table, `onboarding_answers`, holds the athlete's prose. Columns: `user_id`, `step` (`about_you`, `goals`, `level`, `schedule`), `sport_id` (null for `about_you`), `text` varchar(1000), `revision`, `status` (`pending`, `parsed`, `failed`, `skipped`), `parsed` JSONB, `updated_at`. It is unique on `(user_id, step, sport_id)`.
- A send stores the row as `pending`, bumps `revision`, and returns the next card at once.
- The same request then starts the read: `asyncio.create_task`, with the task kept in a set on `app.state` and removed by a done callback. The event loop keeps only weak references to tasks ([Python docs](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task)). The set is cancelled at lifespan shutdown.
- A finished read writes `parsed` only if `revision` still matches. If the athlete went Back and answered again, the stale result is dropped.
- A Skip stores `skipped` with no text and starts no read.

**The summary catches up.**
- `GET /onboarding/summary` renders the `lg-sheet` loading state with `hx-sse:connect` to a stream. The stream follows the chat stream's pattern (ADR-0012: `sse.reconnect:false`, and the element that owns the stream stays in the DOM).
- For each of the athlete's rows that is still `pending` or `failed`, the stream awaits the in-flight task if this process holds one. Otherwise it reads the row itself, once. After that it sends the editable summary.
- So a task lost to a restart or deploy costs only time on the summary. Production runs one Uvicorn process (`Dockerfile` CMD). With more processes, a row might be read twice, which is harmless because of the revision check.
- A read that fails twice leaves its fields empty and the summary still opens.

**One template per step.**
- Four `PromptTemplate`s under `llm/prompts/onboarding.<step>/v1.en.md`, registered in `build_prompt_registry` and the evals CLI: `onboarding.about_you`, `onboarding.goals`, `onboarding.level`, `onboarding.schedule`.
- Deps carry `sport` (label and id), `today` and `timezone`.
- Each has a typed pydantic output, and every field is optional:
  - `about_you`: age, height, weight, limits. Height and weight come as value plus unit (`cm`, `m`, `ft_in`, `kg`, `lb`), and code converts them to cm and kg. Unit conversion is arithmetic, which is code's job.
  - `goals`: a list of `{text ≤ 200, target_date}`.
  - `level`: level and exercises.
  - `schedule`: sessions a week, minutes and days.
- The templates leave unclear details empty (B34).
- The question copy shown on each card lives in Jinja templates, per sport where the mock varies it, not in prompts (ADR-0008).
- The four templates share one settings group: `onboarding_model`, `onboarding_provider_preferences`, `onboarding_call_timeout_seconds` and `onboarding_reasoning_effort`. They do the same kind of short extraction off the critical path, so ADR-0026's per-template latency budget doesn't apply.
  - A template can get its own key later through `/tune-prompt`, if evals show it needs one.
  - The default model is picked on evals (ADR-0009) and on facts from the OpenRouter MCP.
- Every read writes an `llm_calls` row under the athlete's `user_id` (ADR-0018).

**No Jev.** Every output here is an open value: free text, numbers, dates. ADR-0020 is for closed-set picks, so it doesn't apply.

**Confirm.**
- `POST /onboarding/confirm` validates the edited summary.
- In one transaction it writes `athlete_profiles`, `sport_profiles`, `goals`, `user_sports`, `users.timezone` and `users.onboarded_at`, and deletes the athlete's `onboarding_answers` rows.
- That is the confirm step that AI-made data needs (B35). Afterwards no raw prose is kept (B36).

**Flow state comes from the server.**
- The current step is derived from `user_sports` and `onboarding_answers`, so a reload resumes at the next unanswered card.
- The browser's IANA zone is posted with the sport pick, as `static/js/onboarding_timezone.js` does today, and shown on the summary to correct.
- The gate (`web/active_user_gate.py`) still sends an athlete with `onboarded_at IS NULL` to `/onboarding`.

### Invariants

1. Nothing an onboarding read produced reaches the profile tables, `goals` or `user_sports` before the athlete confirms the summary.
2. No onboarding request waits on a model call, except the summary stream.
3. A stale read never overwrites a newer answer. The revision is checked on every write.
4. After confirm, the athlete has no `onboarding_answers` rows.
5. Every onboarding table is user-scoped (ADR-0004 invariant 1) and joins account deletion.
6. Unit conversion and the age-to-birth-year step run in code, never in a prompt.

## Alternatives considered

- **FastAPI `BackgroundTasks`.** These run after the response, but return no handle the summary could await ([FastAPI docs](https://fastapi.tiangolo.com/tutorial/background-tasks/)). A held `asyncio` task does the same and can be awaited.
- **A job queue (Celery, arq, a Postgres queue).** It needs a worker process and its operation on the one Hetzner host (ADR-0023), for four short reads per athlete. The durable answer row plus the catch-up on the summary already survives restarts.
- **One template that reads all the answers at the summary.** The athlete would wait for the whole read. The owner also asked for one small template per step.
- **Store age as given.** Age goes stale; `birth_year` is just as easy to show.

## Consequences

- `CLAUDE.md` gets a Stack line. `.claude/rules/llm.md` and `python.md` get a line each on onboarding reads and the confirm-only write.
- `docs/design/README.md`'s Onboarding row points at `Onboarding v4.dc.html`, with the owner-chosen deviations from the mock:
  - no per-step AI reply (Send = Continue);
  - the voluntary copy;
  - "Empty fields are fine" instead of the Settings line;
  - typed inputs styled as `lg-field`.
- Tickets (M1o):
  - T1: the sport pick, "about you", the background reads, the loading sheet, the summary and confirm.
  - T2: goals, level and schedule.
  - T3: "Add another sport?" and removing the old form steps.
- The old routes, `application/onboarding.py`'s availability and timezone steps, their templates, JS and tests go in T1 and T3.
- Later:
  - drop `weekly_availability` (an owner-approved contract ticket);
  - a Settings profile page reusing the summary partial (post-POC, PRD-0007);
  - feed B33's fields into the reply context (`application/reply.py`'s `_context`), in the ticket that first uses them.
- **Flagged:** the OpenRouter MCP was not authorized when this was written (2026-10-24), so no model is named yet. The owner authorizes it, and T1 picks the default on its first eval baseline.
- **Picked at #254** (owner-approved at the eval gate, 2026-10-24): the `onboarding_*` defaults in `Settings` are `z-ai/glm-5.3-flash` at `low` effort with `{"sort":"latency","quantizations":["fp8","nvfp4"]}`. `onboarding.about_you` v1 scored 98% of its assertions there over 14 cases × 3. At OpenRouter's default routing it scored 89% and failed one case outright: the price sort picks Relace, whose quantization is unknown and which has no structured outputs or required tool choice (`list-model-endpoints`, 2026-10-24). The defaults sit in `Settings`, so no deploy needs a new key; a blank key keeps them.
- **Verified at #254:**
  - The migration stores the integers as `bigint` and the strings as `text`, each bounded by a `CHECK` with the ranges above (`height_cm` 100–250, `weight_kg` 25–300, `birth_year` 1900–2100, `limits` ≤ 300, `text` ≤ 1000). Squawk's default rules refuse `smallint`, `int` and `varchar(n)` on a new table (ADR-0024).
  - `onboarding_answers`' unique key is `NULLS NOT DISTINCT`, so About you, whose `sport_id` is null, is one row per athlete.
  - The sport pick writes `user_sports` and `users.timezone` at once, because the flow state comes from them; confirm writes both again. Invariant 1 covers only what a read produced (owner-confirmed 2026-10-24).
  - htmx 4 sends neither a clicked button's `name` nor its `value`, so a sport row posts its pick through `hx-vals`.
- Europe first, owner-directed 2026-10-25 ([PRD 0008](../prd/0008-ai-training-companion.md) G15). `about_you` must read metric answers reliably, including a decimal comma ("1,78 m", "72,5 kg"). The `ft_in` and `lb` units built in #254 stay but are best effort: an imperial answer the template can't read leaves the field empty for the summary, and a miss on an imperial eval case doesn't block a promotion. Stone isn't a unit; such a weight stays empty.
