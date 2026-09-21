# ADR 0012 — Web UI

| | |
|---|---|
| Status | Proposed |
| Date | 2026-09-16 |
| Related | PRD-0001 (F1–F6, F9, F10, G5, G10), ADR-0002, ADR-0005, ADR-0015 |

## Context

The product is chat-first with a few views (F1–F6). The project owner chose FastAPI with HTMX and daisyUI, and designs made in Claude Design.

## Decision

- **Server-rendered** Jinja2 templates with **htmx 4** and **daisyUI 5** on **Tailwind CSS v4**. No SPA.
- **htmx 4.0.0** (released 2026-08-28) uses `fetch()` and has SSE and streaming in core (`hx-sse:connect`), so no extension is needed. Its npm `latest` tag stays on 2.x until early 2027, so the version is pinned explicitly and the file is vendored under the static folder (no CDN at runtime).
- **Streaming chat** uses FastAPI's native `fastapi.sse.EventSourceResponse` and `ServerSentEvent`; no `sse-starlette` dependency.
- **Layout:** `src/ai_trainer/web/` holds routes, `templates/` (`pages/`, `partials/`, `components/`) and `static/`. A request with the `HX-Request` header gets a partial; any other request gets the full page.
- **CSS** is built at image build time with the Tailwind standalone CLI and daisyUI's `daisyui.mjs` / `daisyui-theme.mjs` bundles — no Node anywhere.
- **Designs** from Claude Design map onto daisyUI components and a daisyUI theme.
- **Shared components** in `templates/components/`: the draft-confirm card (F2), the choice card (F9, ADR-0015) and the route and recommendation cards, which always show their source and fetch date (F10, G10).
- User-facing strings live in templates, not Python, so adding Babel/gettext for Polish later is mechanical (G5).

### Invariants

1. htmx 4 syntax only. Most examples online, and in model training data, are htmx 2: in 4, attribute inheritance needs `:inherited` and 4xx/5xx responses are swapped by default.
2. No Node.js at build or run time; no SPA framework.
3. Styling uses daisyUI components and Tailwind utilities; custom CSS only inside the theme.
4. Every non-GET request carries a CSRF token (ADR-0005).
5. HTMX endpoints return HTML. JSON exists only for the documented API.
6. AI-made changes render as confirm / edit / discard cards (F2) and are never saved automatically.

## Consequences

- `.claude/rules/web.md` points agents at the htmx 4 docs through context7 (`/bigskysoftware/htmx/v4.0.0`).
- E2E tests drive the real UI (ADR-0013).
