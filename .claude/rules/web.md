---
paths:
  - "src/ai_trainer/web/**"
---

# Web rules (ADR-0012, ADR-0015, ADR-0019)

- **htmx 4 syntax only.** Look up the docs through context7 at `/bigskysoftware/htmx/v4.0.0`; don't copy htmx 2 examples. In htmx 4, attribute inheritance needs `:inherited`, 4xx/5xx responses are swapped by default, and SSE is in core (`hx-sse:connect`).
- Requests with the `HX-Request` header get a partial from `templates/partials/<feature>/`; other requests get a full page from `templates/pages/<feature>/`.
- Templates are organized type first, then feature. Layouts go in `layouts/` (`base.html`, `app.html` for the signed-in shell, `bare.html` for sign-in, status and errors). Jinja macros go in `components/`. Name a partial for what it renders (`partials/chat/message.html`), never `<page>_content.html`.
- Style with daisyUI 5 components and Tailwind utilities. Custom CSS only in `static/css/theme.css` (the `trainer-dark` and `trainer-light` themes) and the glass utilities in `static/css/glass.css`. No Node, no runtime CDN, no web fonts.
- Build from the Claude Design mock named in the ticket, using `/implement-design`, and run its parity check before the review gate.
- Mobile first: design at 390 px wide first, with no horizontal scroll, tap targets of at least 44 px, and safe-area insets. Honor `prefers-reduced-motion`, `prefers-reduced-transparency` and `prefers-contrast`.
- `trainer-dark` is the default theme. The theme is applied in `<head>` before first paint from `localStorage["theme"]`; never store it server-side.
- Icons come from the `icon()` macro (vendored Lucide), never inline copies. Client JS is small hand-written files in `static/js/`.
- Every region filled asynchronously gets a skeleton of the same size and layout. The chat shows the thinking indicator from send until the first streamed token; it shows the server's SSE phase when there is one. The words it cycles through live in a template.
- Stream with `fastapi.sse.EventSourceResponse`.
- User-facing strings live in templates, never in Python (G5).
- Every non-GET request carries a CSRF token (ADR-0005).
- AI-made changes render as confirm / edit / discard cards (F2); never save them automatically.
- Ambiguity renders as the choice card, `templates/components/choice_card.html` (F9). It posts option IDs, never label text; an unknown ID is an error, not a guess (ADR-0015).
- Route and recommendation cards always show their source and fetch date (F10, G10).
