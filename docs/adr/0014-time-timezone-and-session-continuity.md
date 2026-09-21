# ADR 0014 — Time, timezone and session continuity

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-16 |
| Related | PRD-0001 (G9, B15, B16), ADR-0003, ADR-0004, ADR-0008, ADR-0015 |

## Context

Logging is conversational, so most dates arrive relative: "yesterday", "Tuesday", "this morning" (B15). A session also arrives in pieces — "I sent 6b", then "6b+", then "6c" — and whether that is one session or three depends on how far apart the messages were (B16). ADR-0003 lists a `clock` adapter and nothing specifies it; ADR-0004 fixes storage at UTC but says nothing about the athlete's local day (G9). Without this, M1 ships a logger that cannot date what it logs.

## Decision

- **A `Clock` port** in `src/ai_trainer/application/`, with one adapter in `src/ai_trainer/adapters/`. Nothing else reads the wall clock.
- **`User.timezone`** — an IANA name (`Europe/Warsaw`), captured from the browser at onboarding, editable in the profile (F6), falling back to UTC when the browser offers nothing.
- **The current date is injected, not fetched.** Every user-facing template's deps model carries `today`, `now_local` and `timezone` (ADR-0008). A date tool does not appear in the app's own pipeline: injection is deterministic, costs no extra call and is testable with a frozen clock. `current_datetime` is exposed as an MCP resource for external clients only (ADR-0010).
- **Extraction output is always absolute.** Alongside `occurred_on`, the extraction templates return `date_source`: `stated`, `relative` or `assumed_today`.
- **Session continuity** — a new log is compared with the athlete's most recent session in the same sport:

| Gap since the last activity | Behaviour |
|---|---|
| within `session_merge_window_min` (settings) | offered as an addition to that session on its draft card |
| same local day, beyond the window | a choice card asks: the same session, or a new one (ADR-0015) |
| a different local day | a new session, no question |

- **Tests** freeze the clock through a fake `Clock`. Midnight boundaries, DST transitions and a message sent at 01:00 about "yesterday evening" are unit tests and eval cases.

### Invariants

1. No wall-clock read outside the `Clock` adapter — no `datetime.now()`, `date.today()` or `utcnow()` anywhere else.
2. Stored timestamps stay timezone-aware UTC (ADR-0004, invariant 5). The local day is derived from `User.timezone` at the edge and never stored as a naive date.
3. A prompt never receives an unresolved relative expression, and extraction output never contains one.
4. A date that was assumed rather than stated is shown on the draft card and can be changed before saving.
5. Merging two logs into one session, or splitting one, happens only after the athlete confirms.

## Consequences

- `.claude/rules/llm.md` carries the deps rule; `.claude/rules/python.md` carries invariant 1.
- `/add-sport` (step 3) gains a relative-date case in every extraction eval dataset.
- M1. The continuity question needs ADR-0015's choice card, so the two ship together.
