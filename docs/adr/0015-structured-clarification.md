# ADR 0015 — Structured clarification and the choice card

| | |
|---|---|
| Status | Proposed |
| Date | 2026-09-16 |
| Related | PRD-0001 (B3, B17, F9), ADR-0008, ADR-0009, ADR-0012, ADR-0014, ADR-0016 |

## Context

B3 promises a clarifying question when input is ambiguous, and ADR-0008 makes that one free-text question. Real ambiguity is usually a small closed set: three crags have a route of the same name, a log could extend yesterday's session or start a new one, an exercise name matches two movements. Typing the answer is slower than pointing at it, and free text has to be parsed again — a second chance to get it wrong (B17, F9). Every sport hits this, so the mechanism belongs in the pipeline, not in a plugin.

## Decision

- **A typed `Clarification`** model in `src/ai_trainer/llm/`, available as an alternative `output_type` for the router and for every specialist:

| Field | Meaning |
|---|---|
| `question` | one sentence, in the persona's voice (G2) |
| `options` | 2–N `ChoiceOption`: `id`, `label`, `detail`, optional `badge` |
| `mode` | `single` or `multi` |
| `allow_free_text` | whether "let me type instead" is offered |
| `pending` | the draft or intent this blocks |

- A specialist that cannot proceed returns `Clarification` instead of its normal output. The dispatcher (ADR-0008) renders it and suspends that intent; the other intents in the message still run (B14).
- **The answer returns option IDs, not text**, in one htmx POST. The resumed run receives the selected options through its deps and continues where it stopped.
- **One component**, `src/ai_trainer/web/templates/components/choice_card.html`, built from daisyUI (ADR-0012): large tap targets, keyboard-selectable, and a confirm button when `mode` is `multi`.
- `detail` always carries what distinguishes the options — the crag, the date, the grade — never a repeated label.
- `max_clarification_options` in settings (default 5). More candidates than that means narrowing first, or asking a different question.

### Invariants

1. One clarification at a time per intent. Never a list of questions.
2. Options are concrete and selectable. Free text is an escape hatch, never the only route.
3. A clarification writes nothing; the draft stays pending until the athlete picks.
4. The resumed run trusts the option `id`. Label text is for display, and an unknown ID is an error, not a guess.
5. Every clarification path has an eval case (ADR-0009) and an integration test asserting that nothing was written.

## Consequences

- ADR-0008's rule "produces one clarifying question" is replaced by this contract; B3 is refined by B17.
- `.claude/rules/web.md` carries the ID-not-label rule; `.claude/rules/llm.md` points specialists at `Clarification`.
- ADR-0014 (same session or not?) and ADR-0016 (which crag?) both depend on it, so it lands in M1.
- Amended at #162 (the first question, "which sport?"), proposed 2026-10-21 for the owner's review with that ticket's PR. These refine the contract above; the Decision and the invariants stand.
  - **Where the model lives.** `Clarification` and `ChoiceOption` are in `src/ai_trainer/domain/clarification.py`, not `llm/`: the dispatcher (application) and the web layer both read them, and neither may import `llm/` (ADR-0003). `question` is still the persona's one sentence, read from a fixed-reply file (`llm/prompts/sport_question/v1.en.md`, sent as written with no model call), never built in Python (ADR-0008).
  - **Options carry copy keys.** A `ChoiceOption` is `id` and `copy_key`, not `label` and `detail`: user-facing text lives in templates (G5), as a sport's `label_key` does. The card resolves the key to the label and the detail that tells options apart (for a sport, its name and its hint in `components/sport_copy.html`). `badge` and `mode: multi` are in the table above but wait for the first question that needs them; only `single` is built.
  - **When a sport is asked.** One `log_session` intent, Jev sure of the request but not of its sport (`FastRoute.single_intent`), no settling context (a recent draft in the conversation, ADR-0026 step 5), and two or more of the athlete's *loggable* sports (an extraction template, ADR-0006) at or above the plausible floor (`router_fast_plausible_sport_floor`). Options are exactly those sports, likeliest first, cut to `max_clarification_options`. The LLM router is not called: it would only guess. An athlete with one sport, or one whose message names its sport, is never asked.
  - **Storage.** The question is saved as the assistant reply to the log (fixed reply `sport_question`) and as an `open` `clarifications` row with `purpose` `sport`, `message_id` the athlete's log and `draft_id` null. No draft and no session exists until a tap. A reload replays the saved reply and its row; it never asks twice.
  - **The answer.** `POST /clarifications/{id}/answer` carries `option_ids`. An ID the question did not offer, or not exactly one, is a 422 (invariant 4); an answered or skipped question is a 409; another user's, a 404. The resumed run is the sport's ordinary extraction on the original message, in the chosen sport. The row becomes `answered` only after that extraction came through, in one conditional UPDATE, and the draft follows in the same task; a failed extraction leaves the card open to tap again, and two taps at once leave one draft. `POST /clarifications/{id}/abandon` is "Let me type instead": the row becomes `abandoned`, nothing else is written, and the composer takes focus.
  - **With a pain mention.** A sport question never delays the safety reply (ADR-0008 invariant 4, G3): when the screen finds pain, the log is drafted in its likeliest sport and the athlete changes it on the card (F2), with no question.

