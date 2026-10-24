---
paths:
  - "src/ai_trainer/web/**"
---

# Web rules (ADR-0012, ADR-0015, ADR-0019)

- **htmx 4 syntax only.** Look up the docs through context7 at `/bigskysoftware/htmx/v4.0.0`; don't copy htmx 2 examples. In htmx 4, attribute inheritance needs `:inherited` and 4xx/5xx responses are swapped by default. SSE (`hx-sse:connect`) is a core extension, vendored as `static/vendor/hx-sse-4.0.0.min.js` and loaded in `layouts/base.html`, not part of `htmx.min.js` (ADR-0012).
- Requests with the `HX-Request` header get a partial from `templates/partials/<feature>/`; other requests get a full page from `templates/pages/<feature>/`.
- Templates are organized type first, then feature. Layouts go in `layouts/` (`base.html`, `app.html` for the signed-in shell, `bare.html` for sign-in, status and errors). Jinja macros go in `components/`. Name a partial for what it renders (`partials/chat/message.html`), never `<page>_content.html`.
- Style with daisyUI 5 components and Tailwind utilities. Custom CSS only in `static/css/theme.css` (the `trainer-dark` and `trainer-light` themes) and the glass utilities in `static/css/glass.css`. No Node, no runtime CDN, no web fonts.
- **New elements are Liquid Glass v4** (ADR-0019, 2026-10-24 amendment; reference: the design system's `Draft Card v2` v4 column and `LIQUID-GLASS-V3.md`).
  - Navigation and floating UI are glass (`glass-*`); content cards are `mat-content` with `lbl-1` / `lbl-2` / `lbl-3` text.
  - Controls are `lg` + `lg-tinted` (one primary per group), `lg-clear` (secondary, chips, icon buttons) or `lg-quiet` (tertiary, destructive); `lg-chip` for 32 px chips with an invisible 44 px target.
  - A list or sheet the athlete reads (the conversation list, a drawer of items) is content: make its surface `mat-content`, so its controls can be `lg` (the selected item an `lg-clear` capsule, its main action `lg-tinted`). Don't keep it on `glass-*` with flat fills; that loses the v4 look (#212, round 1).
  - Never glass on glass: `lg` sits on `mat-content` or the wallpaper only, never on a `glass-*` surface or inside another `lg`; the few surfaces that stay glass (dock, composer) use fills for what sits on them.
  - See what each class looks like in the screens project's [Conversation list v2](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Conversation+list+v2.dc.html) sampler.
  - Don't add new `btn-primary` / `btn-ghost` controls or `glass-regular` cards; existing v1 elements change only in a roll-out ticket.
- Build from the Claude Design mock named in the ticket, using `/implement-design`, and run its parity check; a human approves its screenshots at the acceptance gate, before the review gate (ADR-0001, ADR-0019).
- Mobile first: design at 390 px wide first, with no horizontal scroll, tap targets of at least 44 px, and safe-area insets. Honor `prefers-reduced-motion`, `prefers-reduced-transparency` and `prefers-contrast`.
- `trainer-dark` is the default theme. The theme is applied in `<head>` before first paint from `localStorage["theme"]`; never store it server-side.
- Icons come from the `icon()` macro (vendored Lucide), never inline copies. A sport's icon comes from `SportRegistry`, never a hard-coded name; an assistant reply shows the glyph of its one sport, or the app mark for several sports or none (F15). Client JS is small hand-written files in `static/js/`.
- Every region filled asynchronously gets a skeleton of the same size and layout. The chat shows the thinking indicator from send until the first streamed token; it shows the server's SSE phase when there is one. The words it cycles through live in a template.
- Stream with `fastapi.sse.EventSourceResponse`. Set `hx-config="sse.reconnect:false sse.pauseOnBackground:false"` on a stream that ends on its own, so neither its end nor a backgrounded tab reconnects it, and keep the element that owns `hx-sse:connect` in the DOM until the stream ends: swap its contents (`<template hx type="partial">`), never the element itself (ADR-0012).
- User-facing strings live in templates, never in Python (G5).
- Every non-GET request carries a CSRF token (ADR-0005).
- AI-made changes render as confirm / edit / discard cards (F2); never save them automatically.
- Ambiguity renders as the choice card, `templates/components/choice_card.html` (F9). It posts option IDs, never label text; an unknown ID is an error, not a guess (ADR-0015).
- Route and recommendation cards always show their source and fetch date (F10, G10).
