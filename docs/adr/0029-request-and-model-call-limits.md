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

**Everything else with a session cookie, by that cookie.** **120 requests per rolling minute.** A request with no cookie is not counted here: the gate refuses it before a model runs. `/health` and `/static` are never counted, so a deploy's health check and assets are not.

**Model calls, by user.** The composition root wraps both the text gateway and the decision gateway in one budget: **300 calls per user per rolling hour, and 8 in flight.** A busy first hour — three sports onboarded twice, then forty logs and twenty retries — is about 260 calls, so 300 sits above it. Eight in flight fits one reply's fan-out and a second reply overlapping it. Evals build their own gateways and are not wrapped.

A refused model call is not sent and writes no `llm_calls` row. A call that starts still counts for the hour if the caller cancels it; only the in-flight slot is released. The chat reply uses the existing error row, with its own sentence.

**Request body.** Caddy's `request_body` `max_size` is **256KiB** ([Caddy `request_body`](https://caddyserver.com/docs/caddyfile/directives/request_body), in the stock `caddy:2.11.7` image that production runs; `caddy adapt` reads it as `max_size: 262144`). A larger body is refused with 413 before it reaches the app. There is no upload.

The numbers are `Settings` fields: `auth_requests_per_minute`, `session_requests_per_minute`, `llm_calls_per_user_per_hour`, `llm_calls_in_flight_per_user`. The window reads the `Clock` port (ADR-0014).

The owner also sets a credit limit on the production OpenRouter key. The app cannot see that dashboard. It is the backstop if a limit here is wrong.

### Invariants

1. Production stays one process. The windows are in memory, and adding workers multiplies every cap.
2. Sign-in routes are limited by `CF-Connecting-IP`, then the TCP peer. Other authenticated traffic is limited by the session cookie, never by the TCP peer.
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
