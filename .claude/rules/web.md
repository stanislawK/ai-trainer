---
paths:
  - "src/ai_trainer/web/**"
---

# Web rules (ADR-0012)

- **htmx 4 syntax only.** Look up the docs through context7 at `/bigskysoftware/htmx/v4.0.0`; don't copy htmx 2 examples. In htmx 4, attribute inheritance needs `:inherited`, 4xx/5xx responses are swapped by default, and SSE is in core (`hx-sse:connect`).
- Requests with the `HX-Request` header get a partial from `templates/partials/`; other requests get a full page from `templates/pages/`.
- Style with daisyUI 5 components and Tailwind utilities. Custom CSS only inside the daisyUI theme. No Node.
- Stream with `fastapi.sse.EventSourceResponse`.
- User-facing strings live in templates, never in Python (G5).
- Every non-GET request carries a CSRF token (ADR-0005).
- AI-made changes render as confirm / edit / discard cards (F2); never save them automatically.
