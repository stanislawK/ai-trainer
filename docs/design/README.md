# Design

Claude Design is the visual source of truth, and this folder is the map to it ([ADR-0019](../adr/0019-visual-design-system.md)). The brief for both projects is [claude-design-prompt.md](claude-design-prompt.md).

## Projects

| Project | Purpose | Link |
|---|---|---|
| ai-trainer · Liquid Glass | Design system: `theme.css`, foundations, components, favicon | [claude.ai/design/p/d1ddc556…](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb) |
| ai-trainer · Screens | Every screen in mobile and desktop frames, in both themes | [claude.ai/design/p/a68b0219…](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d) |

Share only `claude.ai/design/...` links here. Never paste a `serve_url`: it carries a short-lived token.

Design-system files: [Foundations](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Foundations.dc.html), [Components Core](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Components+Core.dc.html), [Components Navigation](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Components+Navigation.dc.html), [Components Chat](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Components+Chat.dc.html), [Components Loading](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Components+Loading.dc.html), [Favicon](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Favicon.dc.html).

Choices made in Foundations: accent **1b** (Glacier azure, hue 232) and dock pattern **2c** (no dock on chat; the sections open from a header menu button, and every other screen keeps the dock). The app mark is **3b** (chosen 2026-09-23); the screens still show 3a until they are updated.

## Handoff contract

- `theme.css` in the design-system project defines `[data-theme="trainer-dark"]` and `[data-theme="trainer-light"]` with daisyUI's variable names, plus the glass utilities. In the repo, it becomes `src/ai_trainer/web/static/css/theme.css` (`@plugin "daisyui/theme"` blocks) and `glass.css` (`@utility` blocks).
- Mocks use real daisyUI 5 classes, so their markup moves into `templates/` with sample data replaced by context variables.
- Owner tweaks made in the Claude Design editor land as `__om-edit-overrides` styles. `/implement-design` folds them back into tokens before harvesting.
- Mock-only helpers, not harvested:
  - `icons.js` defines `<lucide-icon name="…">`, which maps 1:1 to the `icon(name)` macro. It also restores `color-scheme` on `<html>`, because the Claude Design runtime pins it to light.
  - Each mock carries the `#lg-refract` SVG filter that `glass-thick` refraction needs. `layouts/base.html` must include it too.
- In the mocks, daisyUI color opacities come only in steps of 10 (`bg-base-content/10`), because the CDN build ships only those.

## Screen → file → ticket

Filled in as screens and tickets are created.

`Ticket` holds only a GitHub issue number, or _tbd_ until the ticket exists. `Milestone` is where the screen is planned (PRD delivery mapping).

| Screen | Claude Design file | Template | Milestone | Ticket |
|---|---|---|---|---|
| Sign-in | [Sign-in](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Sign-in.dc.html) | `pages/auth/sign_in.html` | M0 | _tbd_ |
| Status (pending / disabled) | [Status and errors](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Status+and+errors.dc.html) | `pages/auth/status.html` | M0 | _tbd_ |
| Errors (404, 500, 401, 403) | [Status and errors](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Status+and+errors.dc.html) | `pages/errors/<code>.html` | M0 | _tbd_ |
| App shell (dock, sidebar) | [Components Navigation](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Components+Navigation.dc.html) (design system), [Chat desktop](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Chat+desktop.dc.html) | `layouts/app.html` | M0 | _tbd_ |
| Chat | [Chat](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Chat.dc.html), [Chat desktop](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Chat+desktop.dc.html) | `pages/chat/index.html` | M1 | _tbd_ |
| Onboarding / Settings | [Onboarding](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Onboarding.dc.html), [Settings](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Settings.dc.html) | `pages/onboarding/`, `pages/settings/` | M0 (sign-in, account deletion), M1 (onboarding) | #17 (account deletion) |
| History | [History](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=History.dc.html) | `pages/history/index.html` | M2 | _tbd_ |
| Statistics | [Statistics](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Statistics.dc.html) | `pages/stats/index.html` | M2 | _tbd_ |
| Plans | [Plans](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Plans.dc.html) | `pages/plans/index.html` | M4 | _tbd_ |
| Admin: users | [Admin users](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Admin+users.dc.html) | `pages/admin/users.html` | M0 | #16 |

## Tokens

Copied from the design system's `theme.css` on 2026-09-23. If the owner tweaks the theme in the editor, copy the values again. Colors are oklch; the variable names are daisyUI's. `secondary` and `accent` alias `primary`, so only one accent exists.

| Token | trainer-dark (default) | trainer-light |
|---|---|---|
| `color-scheme` | `dark` | `light` |
| `--color-base-100` | `oklch(17% 0.012 232)` | `oklch(98.5% 0.004 232)` |
| `--color-base-200` | `oklch(14.5% 0.01 232)` | `oklch(96.5% 0.006 232)` |
| `--color-base-300` | `oklch(12% 0.008 232)` | `oklch(92.5% 0.008 232)` |
| `--color-base-content` | `oklch(95.5% 0.006 232)` | `oklch(21% 0.015 232)` |
| `--color-primary` (= secondary, accent) | `oklch(73% 0.12 232)` | `oklch(48% 0.13 232)` |
| `--color-primary-content` | `oklch(18% 0.03 232)` | `oklch(98.5% 0.006 232)` |
| `--color-neutral` | `oklch(27% 0.012 232)` | `oklch(24% 0.014 232)` |
| `--color-neutral-content` | `oklch(96% 0.005 232)` | `oklch(97% 0.004 232)` |
| `--color-info` / `-content` | `oklch(76% 0.09 255)` / `oklch(18% 0.03 255)` | `oklch(50% 0.12 255)` / `oklch(98.5% 0.006 255)` |
| `--color-success` / `-content` | `oklch(77% 0.12 160)` / `oklch(18% 0.03 160)` | `oklch(50% 0.11 155)` / `oklch(98.5% 0.006 155)` |
| `--color-warning` / `-content` | `oklch(83% 0.12 80)` / `oklch(20% 0.04 80)` | `oklch(80% 0.14 80)` / `oklch(24% 0.05 70)` |
| `--color-error` / `-content` | `oklch(71% 0.15 25)` / `oklch(18% 0.03 25)` | `oklch(52% 0.18 27)` / `oklch(98.5% 0.006 27)` |
| `--radius-selector` / `--radius-field` / `--radius-box` | `2rem` / `2rem` / `1.5rem` | same |
| `--size-selector` / `--size-field` | `0.25rem` / `0.275rem` (44px buttons and inputs) | same |
| `--border` / `--depth` / `--noise` | `1px` / `1` / `0` | same |

Glass layer, built only from the variables above:

| Utility | Recipe | Use |
|---|---|---|
| `wallpaper` | `base-100` plus four soft radial fields: `primary` at 30, 14 and 20%, and `base-content` at 7% | Page background |
| `glass-clear` | tint 34%, `blur(12px) saturate(150%)`, 1px rim | Popovers, pills, overlays |
| `glass-regular` | tint 56%, `blur(24px) saturate(170%)`, rim and inner shadow | Cards, bubbles |
| `glass-thick` | tint 70%, `blur(40px) saturate(190%)`; Chromium adds `url(#lg-refract)` | Dock, composer, modals |
| `glass-rim` | `::before` specular gradient ring; needs a positioned element | Raised glass |

Tint is `base-100` in light. In dark it is `base-100` lifted 7% toward `base-content`. Under `prefers-reduced-transparency: reduce` or `prefers-contrast: more`, all glass turns solid and the wallpaper goes flat.

Measured contrast, over the brightest wallpaper spot:
- Body text on glass: dark 10.0–12.4:1, light 11.7–14.4:1.
- Muted (70%) text: at least 5.3:1.
- Text on primary: 8.0:1 in dark, 5.9:1 in light.
- Accent-colored text belongs on glass only, never on the bare wallpaper.
- Warning is never used as text in light (1.8:1).
