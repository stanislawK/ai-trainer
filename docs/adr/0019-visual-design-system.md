# ADR 0019 — Visual design system and design handoff

| | |
|---|---|
| Status | Accepted |
| Date | 2026-09-23 |
| Related | PRD-0002 (G11, F1–F14), PRD-0003 (F15), ADR-0012 (amended alongside), ADR-0013, ADR-0015 |

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
  - Sport glyphs: each sport's icon comes from its plugin in `SportRegistry` (ADR-0006). An assistant reply's avatar is the glyph of the one sport the reply is about, and the app mark when it is about several sports or none (F15).
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
  - The screenshots are saved to files, not just judged in the moment: a human looks at them and confirms the match at the ticket's acceptance gate (ADR-0001) before this check counts as passed. The agent's own read of "no diff" is evidence to present, never the verdict.
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

- Owner-directed amendment, 2026-09-23 (PRD 0003, F15): the reply avatar is the sport glyph instead of an app mark on every reply. The Chat mocks are updated to match.

- ADR-0012 is amended in the same change (layout, custom-CSS invariant, client JS). ADR-0013 records the parity check and the seeded-session helper.
- New skill `/implement-design`. `.claude/rules/web.md` and `tests.md` and the skills `apply-ticket`, `add-endpoint` and `create-tickets` carry the rules above. `CLAUDE.md` lists this ADR.
- The first ticket (template restructure) moves the existing templates into the new layout with no visual change. The theme ticket adds `daisyui-theme.js` to `scripts/build_css.sh` and the Dockerfile's `css-builder` stage, and adds `theme.css` and `glass.css` as `@import`s in `input.css`.
- App icons are rendered from the design system's favicon file with Playwright and committed as PNGs. No Node.
- ⚠ Support for `prefers-reduced-transparency` differs by browser. It ships in Chrome 118 and later ([Chrome blog](https://developer.chrome.com/en/blog/css-prefers-reduced-transparency)) but is off by default in Firefox ([bug 1736914](https://bugzilla.mozilla.org/show_bug.cgi?id=1736914)). Safari's support was not confirmed, which is why `prefers-contrast: more` is a second trigger. Re-check this in the theme ticket.
- Owner-directed amendment, 2026-09-24: the parity check gets a human gate. `mcp__playwright__browser_take_screenshot` is called with an explicit `filename` under the session's scratchpad directory (not the tool's default ephemeral output path) so the files outlive the check, and `/implement-design` asks the human to look at that folder and confirm the match — pausing there rather than folding "screenshots looked right to me" into the review-gate summary as if it were already settled. `.claude/skills/implement-design` and `apply-ticket`'s verify step carry this.
- Owner-directed amendment, 2026-09-29: the parity check's separate human gate (2026-09-24, above) folds into ADR-0001's new acceptance gate. It is now that gate's second question (Approve / Request changes), asked together with the click-through. Screenshots move from the session scratchpad to the gitignored `.acceptance/<issue#>/parity/` at the repo root, where the human can open them from clickable links. `.claude/skills/implement-design`, `apply-ticket` and `.claude/rules/web.md` carry this.
- Checked in the first design step (2026-09-23): daisyUI and Tailwind render correctly inside Claude Design's `.dc.html` runtime. Each mock loads daisyUI's prebuilt CDN CSS, the pinned `@tailwindcss/browser` build (JIT) and `theme.css`. Because the prebuilt daisyUI CSS ships color opacities only in steps of 10, mocks use only those steps ([docs/design/README.md](../design/README.md)).
- Owner-directed amendment, 2026-10-24 (#212): **every new element follows Liquid Glass v4**, the direction the owner approved on the design system's `Draft Card v2` file (its v4 column). The rules and classes live in the design system's `LIQUID-GLASS-V3.md` and `glass-v2.css`; the screens project carries a copy of `glass-v2.css`.
  - **Two layers.** Navigation and floating controls are the functional layer and stay glass (`glass-clear`, `glass-regular`, `glass-thick`). Content (cards in the chat, lists and sheets the athlete reads) moves to the content material `mat-content`, with the label tiers `lbl-1` / `lbl-2` / `lbl-3` (95 / 64 / 45% of `base-content`; `lbl-3` only for non-essential text).
  - **Controls** are `lg` plus one variant: `lg-tinted` for the one primary action in a group, `lg-clear` for secondary actions, chips and icon buttons, `lg-quiet` for tertiary or destructive ones; `lg-chip` is the 32 px size (with an invisible 44 px tap target, invariant 3). A new variant only overrides the `--lg-*` variables and `color`.
  - **No glass on glass.** `lg` may sit on `mat-content` or directly on the wallpaper, never on a `glass-*` surface or on another `lg`. Anything that stays on glass (the composer, the dock) uses fills (`bg-base-content/10`).
  - **Lists and sheets the athlete reads are content.** Their surface is `mat-content`, so their controls are `lg`: the conversation list (#212) is a `mat-content` sheet on a phone and makes the desktop sidebar `mat-content`, with "New conversation" `lg-tinted` and the open conversation and active section `lg-clear` capsules. Its first draft kept the glass drawer with flat fills and was rejected at the acceptance gate as "far from the v4 effect"; the owner chose this from three options in the screens project's `Conversation list v2`, which also shows each v4 class.
  - Radii are concentric (inner radius = outer radius − padding). Colors come only from theme variables, with `light-dark()` for every white or black mix.
  - The closed set of glass utilities grows by `mat-content`, `lbl-1`, `lbl-2`, `lbl-3`, `lg`, `lg-tinted`, `lg-clear`, `lg-quiet` and `lg-chip`, still built only from theme variables, so invariant 1 holds. The superseded proposal classes in `glass-v2.css` (`fill-chip`, `btn-gem`, `btn-plain`, `btn-gem2`, `btn-plain2`, `chip-recess`) and the loader drafts are not harvested.
  - The first web ticket that builds a v4 element (the conversation list from #212's mock, or a draft-card restyle) harvests these classes into `static/css/glass.css` as `@utility` blocks, through `/implement-design`. The pointer-following light (`--mx` / `--my`) is optional: a small `static/js/glass.js` may set it, and without it the light sits centred.
  - Existing v1 elements (`btn-primary` / `btn-ghost` on glass cards, glass-regular header buttons) stay as they are until a roll-out ticket restyles them screen by screen; a ticket that only touches one of them does not restyle it on the side.
  - ⚠ To verify in the harvest ticket: that Tailwind 4 accepts a utility named `lg` next to its `lg:` breakpoint variant and the `lg-*` names beside it; if not, rename the family (e.g. `lgx`) in both `glass-v2.css` and the repo in one change. `light-dark()` needs Safari 17.5+, Chrome 123+ and Firefox 120+; check the PWA's targets.
- Owner-directed amendment, 2026-10-25: **the v1 roll-out is four grouped tickets, not one per screen**, so it lands before the first release. The source is the design system's four v4 component files (Components Core v4, Chat v4, Navigation v4 and Loading v4, listed in [`docs/design/README.md`](../design/README.md)). The tickets are #265 (chat cards), #266 (chat thread), #267 (app shell) and #268 (settings, admin and auth pages), and they block the first release (#227). A screen with no v4 screen mock keeps its existing mock's layout; only its materials and controls change to the v4 component file's.
  - The closed set of glass utilities also covers the other families those files use: `lgs` and `lgs-thick` (floating surfaces), `mat-bubble`, `lg-ghost`, `lg-danger`, the sizes `lg-sm`, `lg-xs` and `lg-icon`, `lg-row`, `lg-mark`, `lg-nav`, `lg-tab`, `lg-dock-item`, `lg-seg-on` and `lg-seg-item`, `lg-field` and `lg-select`, and the thinking indicator's `ld-ball*`. Invariant 1 still holds, because all of them are `@utility` blocks in `glass.css` built only from theme variables. `lgs`, `mat-bubble`, `lg-ghost`, `lg-row`, `lg-mark`, `lg-sm`, `lg-icon`, `lg-field` and `lg-select` were already harvested with #254; each roll-out ticket harvests the rest as it needs them.
  - The thinking indicator becomes Loading v4's glass ball. Its behaviour is unchanged: the SSE phase replaces the cycling word, the elapsed hint appears after 8 s, and under `prefers-reduced-motion` the ball stops and only the text changes.
