# ADR 0005 — Auth and tenancy (POC)

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-16 |
| Related | PRD-0001 (G1, G5, G7, G8, F6, F7, F8), ADR-0004, ADR-0010 |

## Context

The app is multi-user from day one (G1) and users can delete everything (G7). For the POC the project owner chose Google sign-in only, running locally. Anyone with a Google account can complete that sign-in, so access is approval-gated (G8): only people the owner admits can use the app, and admitting them must not need a redeploy.

## Decision

- **Google OAuth 2.0 / OpenID Connect is the only login method.** Use Authlib's Starlette client, registered with `server_metadata_url='https://accounts.google.com/.well-known/openid-configuration'` and scopes `openid email profile`.
- Users are keyed by Google `sub`. Email and name are stored for display and for matching `ADMIN_EMAILS`. No passwords exist.
- Authlib needs Starlette's `SessionMiddleware` (a signed cookie) for OAuth state. That cookie holds only transient OAuth state and an opaque session ID.
- The authenticated session record lives in PostgreSQL, so it can expire and be revoked. The cookie is HttpOnly and SameSite=Lax. `Secure` comes from a setting that defaults to on. Browsers treat `http://localhost` as a secure context, so the default works for local development; serving the POC over plain HTTP to another device (a phone on the LAN) is the one case that needs it turned off for that run.
- Every state-changing request is CSRF-checked; the M0 ticket picks the mechanism and records it here.
- **Access is approval-gated (G8).** Every user has a status: `pending`, `active` or `disabled`. Signing in creates the account as `pending`, and only an `active` user may use the app. This replaces an email allowlist: it needs no redeploy, and a pending user can spend nothing.
- **Admins come from settings.** `ADMIN_EMAILS` is a list, even while it holds one address. It is matched case-insensitively against the Google email, and only when the `email_verified` claim is true. Admin rights are derived per request and never stored as a database role, so removing an address demotes that person immediately. An account whose verified email is in `ADMIN_EMAILS` is created `active`, which bootstraps the first admin — otherwise nobody could approve anyone.
- **Minimal admin view in M0 (F7).** An admin-only page lists users with their status and changes it. An admin cannot change their own status. A richer view comes later.
- **Status changes take effect at once.** Moving a user to `disabled` or `pending` revokes that user's sessions immediately, rather than waiting for them to expire.
- A `pending` or `disabled` user sees a neutral status screen and nothing else (F8). It never reveals anything about other accounts.
- The POC runs locally: the OAuth client uses `http://localhost` redirect URIs and secrets come from `.env`. Hosting, HTTPS and production redirect URIs belong to a later infra ADR.
- `User.locale` defaults to `en` — the seam for Polish (G5).
- Account deletion (G7) removes the user row; cascades remove all user-owned data (ADR-0004) and all sessions.

### Invariants

1. `user_id` comes only from the authenticated server-side session — never from a request body, query string, form field or LLM tool argument.
2. Every route except sign-in, the OAuth callback, sign-out and health requires an authenticated session.
3. Every non-GET request is CSRF-checked.
4. Google access and refresh tokens are not persisted; only `sub`, email and name are.
5. The session cookie's `Secure` flag is off only in local development. Any hosted deployment serves HTTPS with `Secure` on.
6. A new account is created `pending`, unless its verified email is in `ADMIN_EMAILS`. Only an admin changes a status.
7. Every route except sign-in, the OAuth callback, sign-out, health and the status screen requires an `active` user. The check happens when the session is resolved, on every request — never only at sign-in.
8. Admin rights come from `ADMIN_EMAILS` and a verified email, never from a stored role or a request value.
9. Every status change is recorded: actor, target, old status, new status and timestamp.
10. Moving a user out of `active` revokes that user's sessions immediately.

## Consequences

- Superseded when more login methods or hosting arrive.
- Tests cover: unauthenticated access is refused, tenancy across users, CSRF rejection, and complete account deletion.
- Tests also cover: a new account is `pending` and blocked from every feature; activation grants access; moving out of `active` ends live sessions at once; a non-admin cannot reach the admin view or its actions; admin rights follow `ADMIN_EMAILS` rather than the database; an admin cannot change their own status.
- Admin-side deletion of a pending account joins G7 in a later change; M0 keeps self-deletion.
- MCP tools resolve the user from this session (ADR-0010).
