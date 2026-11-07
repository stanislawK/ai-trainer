# ADR 0029 — Request and model-call limits

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-25 |
| Related | PRD-0008 (G8, G12), ADR-0014, ADR-0018, ADR-0023 |

## Context

Production is one Uvicorn process on one instance (G12, ADR-0023). Every model call is recorded in `llm_calls` (ADR-0018) and nothing reads that record back to refuse the next one. A signed-in session can open as many reply streams as it likes. Each log is about four calls: the safety screen, the fast router, then a router or a specialist and an extraction. Onboarding adds one text call per answer.

`/auth/login`, `/auth/callback` and `/sign-in` are exempt from the active-user gate. There is no password, and a new Google account stays `pending` until an admin activates it (G8), so a stranger cannot spend the model. Those three routes are still the only ones a stranger can usefully hammer. The callback parses the query, checks the OAuth `state`, and a real consent inserts a user.

ADR-0018 left Redis for the external-lookup budget (B22). Those lookups are not in the running app, and this instance does not need another service to count requests.

## Decision

**Count in the process.** One Uvicorn process means an in-memory window is the whole limit. A restart clears it, and only a deploy restarts production. No Redis. The keys come from the request (a cookie, an address), so a key with nothing left in its window is swept at most once per window; memory holds only the keys of the last two windows.

**Sign-in routes, by address.** `GET` or any method on `/auth/login`, `/auth/callback` and `/sign-in` allows **10 requests per address per rolling minute**. The address is Cloudflare's `CF-Connecting-IP`, which Cloudflare overwrites. The origin accepts only Cloudflare (Authenticated Origin Pulls and the firewall, ADR-0023), and Caddy's `reverse_proxy` forwards the header. When the header is missing or not a single address, the TCP peer is used, which is the local client in development. `/auth/logout` is not in this set: it already requires the CSRF cookie.

**Token routes, by token and by failing address.** The routes under `/api/admin/knowledge/` (ADR-0036) are never keyed by a cookie. Two limits apply:
- **By token:** **120 requests per token per rolling minute**, keyed by the SHA-256 of the bearer.
- **By failing address:** **10 failed authentications (401) per address per rolling minute**, by the same address as the sign-in routes. Once an address has used them up, every request it sends to these routes is refused with 429 before its token is read, until the window frees. A request that authenticates is never counted, nor is a 403, which carries a real token whose user lost admin. A stranger cycling made-up tokens is therefore slowed down, though each made-up token is a new key for the per-token limit (owner-directed amendment, 2026-11-04, below).

**Everything else with a session cookie, by that cookie.** **120 requests per rolling minute.** A request with no cookie is not counted here: the gate refuses it before a model runs. `/health` and `/static` are never counted, so a deploy's health check and assets are not.

**Model calls, by user.** The composition root wraps both the text gateway and the decision gateway in one budget: **300 calls per user per rolling hour, and 8 in flight.** A busy first hour — three sports onboarded twice, then forty logs and twenty retries — is about 260 calls, so 300 sits above it. Eight in flight fits one reply's fan-out and a second reply overlapping it. Evals build their own gateways and are not wrapped.

A refused model call is not sent and writes no `llm_calls` row. A call that starts still counts for the hour if the caller cancels it; only the in-flight slot is released. The chat reply uses the existing error row, with its own sentence.

**Request body.** Caddy's `request_body` `max_size` is **256KiB** ([Caddy `request_body`](https://caddyserver.com/docs/caddyfile/directives/request_body), in the stock `caddy:2.11.7` image that production runs; `caddy adapt` reads it as `max_size: 262144`). A larger body is refused with 413 before it reaches the app. The one exception is the admin's knowledge upload, `POST /admin/knowledge/sources` (ADR-0033): it gets **100MB**, which `caddy adapt` reads as `max_size: 100000000`, Cloudflare's own body cap on the Free and Pro plans ([Cloudflare limits](https://developers.cloudflare.com/workers/platform/limits/), "request body size limits depend on the domain's Cloudflare plan", read 2026-11-02). The app enforces `knowledge_max_upload_mb` below that. Two named matchers, the upload and `not` the upload, exclude each other, because two `request_body` handlers that both match a request both apply and the smaller wins.

The numbers are `Settings` fields: `auth_requests_per_minute`, `session_requests_per_minute`, `token_requests_per_minute`, `token_failures_per_address_per_minute`, `llm_calls_per_user_per_hour`, `llm_calls_in_flight_per_user`. The window reads the `Clock` port (ADR-0014).

The owner also sets a credit limit on the production OpenRouter key. The app cannot see that dashboard. It is the backstop if a limit here is wrong.

### Invariants

1. Production stays one process. The windows are in memory, and adding workers multiplies every cap.
2. Sign-in routes are limited by `CF-Connecting-IP`, then the TCP peer. The token routes are limited by token, and their failed authentications by that same address. Other authenticated traffic is limited by the session cookie, never by the TCP peer.
3. `/health` and `/static` are not rate limited.
4. Text calls and decision calls share one per-user budget. A refused call is not sent and writes no `llm_calls` row.
5. Evals are not capped. Only the gateways the composition root wraps are.

## Alternatives considered

- **Redis** — durable across a restart, and what ADR-0018 left open for B22. This instance has nothing else that needs it, and a restart is a deploy.
- **A Cloudflare rate-limiting rule only** — not in the repo, and it cannot see that one reply is several model calls.
- **A custom Caddy build with a rate-limit module** — the stock image has no request-rate directive. `request_body` is in the stock image, so the body cap uses that.

## Consequences

- `CLAUDE.md`, `.claude/rules/python.md` and `.claude/rules/llm.md` name the cap.
- `deploy/RUNBOOK.md` tells the owner to set the OpenRouter key's credit limit, and to run `install.sh` before a release that needs the new Caddyfile.
- B22 stays unimplemented until external lookups exist. It does not reuse this window.
- Owner-directed amendment, 2026-11-02 (#422's plan gate): the knowledge upload route gets its own 100MB body cap (*Request body* above). `knowledge_max_upload_mb` is at most 90 (MiB), so the file and the form's other fields fit under Caddy's 100,000,000 bytes. The release that ships it needs the new Caddyfile, so the owner runs `install.sh` before it, as for any Caddyfile change.
- Amended by [ADR-0036](0036-knowledge-ingestion-at-scale.md), accepted 2026-11-03: the token routes are limited to 120 requests per token per rolling minute, alongside the cookie limit. Ingestion's embedding calls run in the `ingest` container, outside `llm_calls_per_user_per_hour`, bounded by its concurrency of 1 and the OpenRouter credit limit; they still write `llm_calls` rows. Files staged by token go to R2 by presigned PUT, so the 256KiB cap holds on those routes.
- Owner-directed amendment, 2026-11-04 (#457's review): the token routes also count failed authentications per address (*Token routes* above, invariant 2).
  - **Why.** The per-token limit keys on whatever bearer arrives, so a fresh made-up token per request is never slowed. Guessing a 256-bit token isn't feasible. But each token-shaped guess costs a database lookup, two indexed reads of about 0.01 ms each (`EXPLAIN ANALYZE` on `ai_trainer_wt457`, 2026-11-04), so a flood from one address is worth stopping.
  - **No index or cache.** `admin_tokens.token_hash` is already unique, and so indexed, and the user is read by primary key.
  - **Owner, 2026-11-04.** The owner picked this over leaving it as it was, a per-address limit on every token request, and a Cloudflare rule.
  - **Limits of the fix.** A per-address limit doesn't stop a flood spread over many addresses; Cloudflare in front is the backstop for that.
  - **Settings.** `token_failures_per_address_per_minute` defaults to 10, as on sign-in.
  - **Ticket.** It is implemented in #470, after #457.
- Amended at #429 (plan gate 2026-11-07, review the same day): the exercise search's query embedding is charged to the model-call budget. It runs on the log path, outside any tool loop's slot, and a log naming N new exercises makes N calls at once, so it counts like a text or decision call (`BudgetedQueryEmbeddings`); a refused call is not sent and the shortlist falls back to trigram matching. The embedding calls inside a specialist's tool loop stay under that loop's one slot, and evals are not capped. See ADR-0031's #429 amendment.
