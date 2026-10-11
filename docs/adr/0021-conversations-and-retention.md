# ADR 0021 — Conversations and retention

| | |
|---|---|
| Status | Proposed |
| Date | 2026-10-11 |
| Related | PRD-0004 (B13, B23, B24–B26, F16, F17, G1, G7, G13), ADR-0003, ADR-0004, ADR-0008, ADR-0014, ADR-0019, ADR-0022, ADR-0024 |

## Context

Each user's chat is one flat stream in `chat_messages` (ADR-0008, amendment 2026-09-29). `application/chat.py` renders the latest 50 rows. `application/reply.py` gives the router and specialists the last `chat_history_turns` rows across the user's whole chat. So the athlete can't start over, a question from last week leaks into today's prompt, and B23's "current conversation" has no boundary. PRD 0004 adds conversations (B24), a list to return to them (B25, F17) and a lifetime after which they are deleted (B26). Chat text is the most personal data the app keeps, so it shouldn't outlive its use.

## Decision

- **A `conversations` table**, user-owned (ADR-0004, invariant 1):
  - Columns: `id` (UUID), `user_id` (NOT NULL, FK `users` `ON DELETE CASCADE`), `title` (text, nullable), `created_at`, `last_message_at` (`timestamptz`, NOT NULL).
  - Index: `(user_id, last_message_at DESC)`, which serves both the list (B25) and the purge.
- **`chat_messages.conversation_id`** is NOT NULL, FK `conversations` `ON DELETE CASCADE`, and indexed as `(conversation_id, created_at)`. `user_id` stays on the row, so every query is still scoped by user (G1).
- **The migration** is one revision in expand → backfill → contract order:
  1. Add `conversation_id` as nullable.
  2. Create one conversation per user that has messages, with `last_message_at` set to that user's latest message.
  3. Assign the messages to it.
  4. Set the column NOT NULL.

  It runs before the first production deploy. It still follows ADR-0024 so the pattern is set.
- **Ports.** `ChatRepositoryPort` methods take `conversation_id` next to `user_id`. A new `ConversationRepositoryPort` covers create, get, list_recent, touch and purge_before. A conversation id from the URL is always looked up with the session's `user_id`. Another user's id returns 404, never 403.
- **Prompt history** is `chat_history_turns` messages from the current conversation only (B13, B24). A new conversation sends the router and specialists no turns. The athlete's profile and sessions still reach the prompt through deps (ADR-0008).
- **Routes** (ADR-0012):
  - `GET /` opens the most recent conversation, or an empty chat if there is none.
  - `GET /c/{id}` opens a given conversation.
  - `GET /c/new` shows an empty chat (F16).
  - The composer posts to the conversation it shows.
  - A conversation is created lazily, on its first message, so "New conversation" never stores an empty row.
- **Title**: the first athlete message, whitespace-collapsed and truncated to 60 characters in code. No model call is made. Titles are display text, never instructions.
- **Last activity**: `last_message_at` is set from the `Clock` port (ADR-0014) whenever a message or a reply part is saved. A continued conversation moves to the top of the list (B25).
- **Retention (B26)**:
  - `conversation_retention_days` lives in settings (default 14, minimum 1).
  - Expiry is enforced in two places:
    - **On read:** every list and get filters on `last_message_at >= now − retention`, so an expired conversation disappears right away, even before the purge runs.
    - **On purge:** `ai-trainer purge-conversations` is a CLI console script that hard-deletes expired conversations. Messages go by cascade. The CLI reports a count and logs no content. Production runs it daily from the host scheduler (ADR-0023).
- **Saved training data never points at chat.** No training-session column references `conversations` or `chat_messages` (ADR-0022), so a purge cannot touch it. The original text a session needs is copied into the session (`raw_text`, B2).
- **UI**: F16 and F17 are built from their Claude Design mock with `/implement-design` (ADR-0019). The conversation list is a drawer on a phone and a sidebar section on desktop.

### Invariants

1. Every chat message belongs to exactly one conversation of the same user.
2. Prompt history never crosses a conversation boundary.
3. An expired conversation is never listed, opened or used as prompt history, whether or not the purge has run.
4. Purging conversations never deletes or changes a training session, a draft that has been confirmed, a goal or a profile row.
5. Deleting an account removes all of the user's conversations by cascade (G7).

## Alternatives considered

- Soft-delete (archive) past the window — keeps personal text the athlete can no longer see, and G7 would still have to cover it. The owner chose hard delete (2026-10-11).
- An in-app background task for the purge — every app worker would run it, and it's harder to observe. A CLI plus the host scheduler is one process, idempotent and testable.
- LLM-written titles — they cost a call per conversation and need an eval dataset (G6), for little gain in the POC.

## Consequences

- ADR-0008's history rule now reads "within the current conversation". Amend its Consequences when this ADR is Accepted.
- `.claude/rules/python.md` tenancy guidance also applies to `conversation_id`. A test proves user A can't open, continue or purge user B's conversation.
- Tests freeze the `Clock` for the retention boundary: day 14 is still visible, day 14 plus 1 second is not.
- F17 needs a Claude Design mock before its ticket (`docs/design/README.md` row to add). The Chat mock already has the "New conversation" control (#78 note).
- Drafts and pending clarifications are scoped to a conversation (ADR-0022). An unconfirmed draft dies with its conversation, and that is intended: an expired conversation's draft is stale.
