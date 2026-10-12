---
name: add-decision
description: Checklist for adding a Jev decision set (classify, route, gate, score or a closed-set draft field) behind DecisionGatewayPort — question file, state built in code, dataset first, thresholds calibrated on evals, a deterministic fallback, and accounting tests.
argument-hint: "[decision-id]"
---

# Add decision: $ARGUMENTS (ADR-0020)

Precondition: the decision gateway exists (`DecisionGatewayPort`). The owner accepted Jev's retention terms for every decision (ADR-0020 Consequences, 2026-10-12), so athlete text may go in the state, never identity (invariant 9). Use Jev wherever a step only picks and it helps; the uses aren't a fixed list.

1. **Decision or generation?** Jev picks; it does not write. The step should end in a yes/no, one option out of a known set (at most 255), or a level on an ordered rubric (at most 10). A draft field whose value comes from a fixed set (a climbing kind or style, a sport, a catalogue exercise) counts; the athlete still confirms the draft. If the step must produce text, open values (km, grade strings, reps, names) or a calculation, use an LLM template or code instead and stop here.
2. **Question file.** Write `src/ai_trainer/llm/decisions/<id>/v1.en.yaml`:
   - **Goal.** Describe the material Jev is reading.
   - **One entry per question.** Each entry has a `kind`, which is `probability`, `choice` or `score`.
   - **Write criteria and option descriptions like a brief for a new hire.** Jev never sees field or option names, so the description carries all the meaning.
   - **Keep rubric levels in real order from 0,** least to most, with each level described in words.
   - **Split compound questions.** A compound decision becomes several questions in one set. They are answered in parallel and can't see each other.
   - **Runtime options.** When the options depend on the athlete (their sports, this week's planned sessions), the file holds the descriptions and code filters them at runtime.
3. **State built in code.** Assemble the state from typed data: the message, recent turns, the athlete's sports and other context. Pre-compute every number and date, such as days since the last session, so Jev never does arithmetic or date math. Keep it under 32k tokens. Never put identity in the state: no name, email, or user, conversation or message ID.
4. **Dataset first.** Write `evals/datasets/<id>.yaml` with expected labels for code-graded checks. A low-risk decision needs at least 10 cases: one whose wrong answer the athlete sees and fixes before anything is saved (a draft field, a choice card's options), or that costs only a fallback call. A decision that can misroute a message unseen, or that touches safety, needs at least 20 (ADR-0020 invariant 6). Cover the easy cases, near-ties between two options, adversarial or played-down safety mentions, a single-sport athlete, and a multi-request message where that applies. Reuse an existing dataset's cases when the labels already fit; `router.yaml` fits `router_fast`.
5. **Calibrate with `/tune-prompt <id>`.** The eval run records each case's probabilities. Pick each threshold from the cost of each kind of error, not a round number.
   - A wrong auto-route only costs a fallback.
   - A missed pain mention costs safety, so the safety screen's threshold leans low.

   Thresholds go in `Settings`, one field per decision, documented with what a miss costs. The baseline records them.
6. **Reading answers.** Gates read the probability from a bounded `float` or `provider_details` and never use the library's 0.5 `bool` cut. Choices are indexed by option name, never by position. Treat probabilities as jittery: a value right at a threshold may land on either side on the next call.
7. **Fallback.** Wire a deterministic path for timeouts, errors and below-threshold answers: the existing LLM path, the LLM extraction's own value for a draft field, or the safe side of the gate. A safety decision only adds the safety intent, never removes it.
8. **Tests** (with `/run-tdd`), using a fake `DecisionGatewayPort` or a fake `DecisionModel`, never a real Jev call:
   - the loaded output type matches the file;
   - no question text appears in Python (`scripts/check_prompt_literals.py`);
   - every outcome branch: above threshold, below threshold, timeout, error;
   - exactly one `llm_calls` row per call, with the decision ID and version, and `cost` NULL when the response carries none;
   - the fallback runs when it should.
9. **Wire it in** only after the baseline is committed (ADR-0020 invariant 6). Record any latency it adds on the request path in the ticket's acceptance script.
