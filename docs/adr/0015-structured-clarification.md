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
