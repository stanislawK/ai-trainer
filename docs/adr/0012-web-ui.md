# ADR 0012 — Web UI

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-16 |
| Related | PRD-0001 (F1–F6, F9, F10, G5, G10), ADR-0002, ADR-0005, ADR-0015, ADR-0019 |

## Context

The product is chat-first with a few views (F1–F6). The project owner chose FastAPI with HTMX and daisyUI, and designs made in Claude Design.

## Decision

- **Server-rendered** Jinja2 templates with **htmx 4** and **daisyUI 5** on **Tailwind CSS v4**. No SPA.
- **htmx 4.0.0** (released 2026-08-28) uses `fetch()` and has SSE and streaming in core (`hx-sse:connect`), so no extension is needed. Its npm `latest` tag stays on 2.x until early 2027, so the version is pinned explicitly and the file is vendored under the static folder (no CDN at runtime).
- **Streaming chat** uses FastAPI's native `fastapi.sse.EventSourceResponse` and `ServerSentEvent`; no `sse-starlette` dependency.
- **Layout:** `src/ai_trainer/web/` holds routes, `templates/` and `static/`. Templates are organized type first, then feature: `layouts/`, `pages/<feature>/`, `partials/<feature>/` and `components/` (ADR-0019). A request with the `HX-Request` header gets a partial; any other request gets the full page.
- **CSS** is built at image build time with the Tailwind standalone CLI and daisyUI's `daisyui.js` Tailwind plugin bundle, loaded via a CSS `@plugin` directive — no Node anywhere. The separate `daisyui-theme.js` bundle defines the custom `trainer-dark` and `trainer-light` themes (ADR-0019); #8 shipped only daisyUI's built-in themes.
- **Designs** from Claude Design map onto daisyUI components and a daisyUI theme; the handoff is ADR-0019.
- **Client JS** is limited to small hand-written files under `static/js/` with no build step, plus vendored libraries under `static/vendor/` (htmx; Chart.js from M2).
- **Shared components** in `templates/components/`: the draft-confirm card (F2), the choice card (F9, ADR-0015) and the route and recommendation cards, which always show their source and fetch date (F10, G10).
- User-facing strings live in templates, not Python, so adding Babel/gettext for Polish later is mechanical (G5).

### Invariants

1. htmx 4 syntax only. Most examples online, and in model training data, are htmx 2: in 4, attribute inheritance needs `:inherited` and 4xx/5xx responses are swapped by default.
2. No Node.js at build or run time; no SPA framework.
3. Styling uses daisyUI components and Tailwind utilities; custom CSS only inside the theme and the glass utilities of ADR-0019.
4. Every non-GET request carries a CSRF token (ADR-0005).
5. HTMX endpoints return HTML. JSON exists only for the documented API.
6. AI-made changes render as confirm / edit / discard cards (F2) and are never saved automatically.

## Consequences

- `.claude/rules/web.md` points agents at the htmx 4 docs through context7 (`/bigskysoftware/htmx/v4.0.0`).
- E2E tests drive the real UI (ADR-0013).
- CSS build gotcha, found at #8: Tailwind v4's automatic source detection respects `.gitignore` to exclude things it shouldn't scan — but the Docker `css-builder` stage has no `.git`/`.gitignore` in its minimal build context, so with automatic detection left on, the daisyUI plugin bundle (`daisyui.js`, sitting next to `input.css` so `@plugin` can load it) gets swept up as a "source" too, and its own literal class-name strings bloat the compiled CSS roughly 6x (verified: 371KB vs 60KB) with unused component styles — a build that is non-deterministic across environments depending on gitignore presence. Fix: `input.css` disables automatic detection outright (`@import "tailwindcss" source(none);`) and scans only the explicit `@source "../../templates"`, making the build deterministic regardless of `.gitignore`.
- Owner-directed amendment, 2026-09-23 (design session, ADR-0019): the Layout decision moves to type-then-feature folders; invariant 3 also allows the ADR-0019 glass utilities; a Client JS decision is added; the `daisyui-theme.js` line now names the custom themes. Everything else is unchanged.
- CSS build flakiness gotcha, found at #37: `scripts/build_css.sh`'s two `curl -f` downloads (the Tailwind CLI binary, `daisyui.js`) occasionally hit a transient 4xx/5xx from GitHub Releases on a fresh CI runner (`exit code: 22`) — invisible before #37 because no CI job ever built the Docker image (`docker compose build`) until the e2e job did (ADR-0013). Both `curl` calls now carry `--retry 3 --retry-delay 2 --retry-connrefused`; a persistent failure (bad version pin, real outage) still fails after the retries, it just no longer trips on one flaky response.
