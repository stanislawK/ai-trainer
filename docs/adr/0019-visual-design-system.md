# ADR 0019 — Visual design system and design handoff

| | |
|---|---|
| Status | Approved |
| Date | 2026-09-23 |
| Related | PRD-0002 Draft (G11, F1–F14), ADR-0012 (amended alongside), ADR-0013, ADR-0015 |

## Context

ADR-0012 fixes the stack (Jinja2, htmx 4, daisyUI 5, Tailwind 4, no Node). It says designs come from Claude Design, and defers the custom theme until one exists. The UI is now due to move past placeholders (G11, F11–F14). Nothing yet defines the look, how a design gets from Claude Design into templates, or how the result is checked. The project owner chose these in the 2026-09-23 design session.

## Decision

- **Look.** Apple Liquid Glass, with the macOS Dock as the reference: translucent surfaces over a CSS-only, low-chroma wallpaper, frosted blur with raised saturation, a specular top rim, and monochrome glyphs. There is **one accent color**. Sports are told apart by icon and label, never by color.
- **Themes.** Two custom daisyUI themes, `trainer-dark` (`default: true; prefersdark: true`) and `trainer-light`. Both are defined with `@plugin "daisyui/theme"` blocks in `src/ai_trainer/web/static/css/theme.css`, loaded by `input.css` through daisyUI's `daisyui-theme.js` bundle, and use oklch colors. Tailwind's `dark:` variant is bound to `trainer-dark` with `@custom-variant`. Built-in daisyUI themes are disabled (`themes: false`).
- **Glass layer.** `static/css/glass.css` defines a closed set of Tailwind `@utility` classes (`glass-clear`, `glass-regular`, `glass-thick`, `glass-rim`, `wallpaper`), built only from theme variables.
  - Baseline everywhere: `backdrop-filter: blur() saturate()` plus the rim highlight and an inner shadow.
  - Chromium also gets an SVG displacement "refraction" on the dock and composer, gated by `@supports`, because only Chromium accepts SVG filters in `backdrop-filter` ([kube.io](https://kube.io/blog/liquid-glass-css-svg/)).
  - Under `prefers-reduced-transparency: reduce` or `prefers-contrast: more`, glass becomes solid tinted surfaces.
- **Theme persistence.** The choice lives in the browser only, in `localStorage["theme"]`. An inline script in `layouts/base.html`'s `<head>` sets `data-theme` before first paint, and falls back to `trainer-dark` when nothing is stored or storage throws. A theme toggle updates `<html data-theme>`, the storage key and `<meta name="theme-color">`. Nothing is stored server-side.
- **Type and icons.**
  - Fonts: the system stack (`-apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif`; `ui-monospace` for tabular numbers). No web fonts.
  - Icons: [Lucide](https://lucide.dev) SVGs, vendored under `static/icons/lucide/` and rendered through a Jinja `icon(name)` macro in `templates/components/icon.html`.
- **Installability.** `static/manifest.webmanifest` (`display: standalone`, `theme_color`, 192/512 and maskable icons), an SVG favicon, a 180 px `apple-touch-icon`, and `viewport-fit=cover` with safe-area insets. **No service worker.**
- **Templates.** Organized type first, then feature:
  - `templates/layouts/` holds `base.html`, `app.html` (the signed-in shell) and `bare.html` (sign-in, status and errors).
  - `pages/<feature>/<view>.html` and `partials/<feature>/<fragment>.html`. A partial is named for what it renders (`partials/chat/message.html`), never `<page>_content.html`.
  - `components/` holds Jinja macros only.
- **Client JS.** Small hand-written files under `static/js/`, with no build step: `theme.js`, then `thinking.js` and `autocomplete.js` (M1), and `charts.js` (M2). Charts use vendored Chart.js (UMD under `static/vendor/`), themed from the daisyUI CSS variables.
- **Loading and thinking.**
  - Every region filled asynchronously has a skeleton the size and layout of the real element.
  - From send until the first streamed token, the chat shows the thinking indicator. It shows the phase the server sends over SSE when there is one, and otherwise cycles sport-flavored words kept in a template (G5). It degrades to a static indicator under `prefers-reduced-motion`.
- **Design handoff.**
  - **Source of truth.** Claude Design is the visual source of truth, in two projects: the design system and the screens. Links and a screen → file → ticket map live in [`docs/design/README.md`](../design/README.md).
  - **Mocks.** Mocks use real daisyUI 5 classes and a shared `theme.css` that defines both themes as `[data-theme]` variable blocks, daisyUI's CDN form. Tailwind and daisyUI come from a pinned CDN inside the mocks only.
  - **Harvest into the repo** with `/implement-design`. Theme blocks become `@plugin "daisyui/theme"` blocks; the variable names are unchanged. Mock markup becomes Jinja pages, partials and macros.
- **Verification.**
  - During a web ticket's verify step, a Playwright MCP parity check opens the mock (via `render_preview`) and the running app at 390×844 and 1440×900, in both themes. It compares screenshots side by side and probes key computed styles.
  - Committed pytest-playwright specs assert behavior, not pixels (ADR-0013).

### Invariants

1. The only custom CSS is the two daisyUI theme blocks and the glass utilities in `glass.css`. Everything else is daisyUI classes and Tailwind utilities.
2. The default theme is `trainer-dark`. A stored choice is applied before first paint, so the page never flashes the other theme.
3. Text meets 4.5:1 contrast on glass in both themes, and tap targets are at least 44 px. Every page works at 390 px wide without horizontal scrolling.
4. Glass has an opaque fallback under `prefers-reduced-transparency` and `prefers-contrast: more`, and motion honors `prefers-reduced-motion`.
5. No runtime CDN, web font or remote icon. Everything the app loads is served from `/static` (ADR-0012).
6. A web ticket that implements a designed screen names its Claude Design file, and passes the parity check before the review gate.

## Alternatives considered

- Free-form Claude Design markup: every screen would need rewriting into daisyUI, and the result would drift from the mock.
- Full SVG refraction everywhere: Safari and iOS can't render it, and the PWA runs there ([kube.io](https://kube.io/blog/liquid-glass-css-svg/)).
- Theme stored per user in the database: it needs a migration and an endpoint, for little gain in a POC.
- Server-rendered SVG charts: no interactivity, and more to build than vendored Chart.js.
- Committed pixel snapshots: flaky across operating systems and GPUs.

## Consequences

- ADR-0012 is amended in the same change (layout, custom-CSS invariant, client JS). ADR-0013 records the parity check and the seeded-session helper.
- New skill `/implement-design`. `.claude/rules/web.md` and `tests.md` and the skills `apply-ticket`, `add-endpoint` and `create-tickets` carry the rules above. `CLAUDE.md` lists this ADR.
- The first ticket (template restructure) moves the existing templates into the new layout with no visual change. The theme ticket adds `daisyui-theme.js` to `scripts/build_css.sh` and the Dockerfile's `css-builder` stage, and adds `theme.css` and `glass.css` as `@import`s in `input.css`.
- App icons are rendered from the design system's favicon file with Playwright and committed as PNGs. No Node.
- ⚠ Support for `prefers-reduced-transparency` differs by browser. It ships in Chrome 118 and later ([Chrome blog](https://developer.chrome.com/en/blog/css-prefers-reduced-transparency)) but is off by default in Firefox ([bug 1736914](https://bugzilla.mozilla.org/show_bug.cgi?id=1736914)). Safari's support was not confirmed, which is why `prefers-contrast: more` is a second trigger. Re-check this in the theme ticket.
- ⚠ Whether Tailwind's browser build plus daisyUI render correctly inside Claude Design's `.dc.html` runtime is checked in the first design step. If they don't, mocks link prebuilt daisyUI CSS plus `theme.css` without Tailwind JIT.
