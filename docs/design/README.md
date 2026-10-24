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

Filled in as screens and tickets are created. The design-foundation tickets that build no single screen: #36 (template layout), #37 (e2e specs), #38 (themes), #39 (icons), #42 (installability).

`Ticket` holds only a GitHub issue number, or _tbd_ until the ticket exists. `Milestone` is where the screen is planned (PRD delivery mapping).

| Screen | Claude Design file | Template | Milestone | Ticket |
|---|---|---|---|---|
| Sign-in | [Sign In](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Sign+In.dc.html) (design system; replaces the Screens project's `Sign-in` file) | `pages/auth/sign_in.html` | M0 | #43, #59 |
| Status (pending / disabled) | [Status and errors](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Status+and+errors.dc.html) | `pages/auth/status.html` | M0 | #44, #58 |
| Errors (404, 500, 401, 403) | [Status and errors](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Status+and+errors.dc.html) | `pages/errors/<code>.html` | M0 | #44, #58 |
| App shell (dock, sidebar) | [Components Navigation](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Components+Navigation.dc.html) (design system), [Chat desktop](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Chat+desktop.dc.html) | `layouts/app.html` | M0 | #40, #58 |
| Chat | [Chat](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Chat.dc.html), [Chat desktop](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Chat+desktop.dc.html) | `pages/chat/index.html` | M1 | #78 (history and composer; starter chips, "New conversation" and the reply states wait for later tickets), #82 (the streamed reply: the assistant row and the streaming state; its error row, which has no mock, reuses the "Not sent / Retry" pattern), #83 (the thinking indicator: the mock's state 3, from send until the first token, with the SSE phase label and an elapsed-time hint after 8 s), #84 (the reply avatar: the sport glyph, else the app mark; the app mark is the mock's `trending-up` until the 3b mark replaces it), #116 (the composer's Stop: the mock's states 3 and 4 swap Send for a round glass Stop; the "Stopped" row has no mock and reuses the error row's pattern; the desktop mock has no streaming frame, so desktop reuses the mobile button), #85 (composer suggestions, F13: the mock's state 1 starter chips and state 7 popover, in `partials/chat/starter_chips.html` and `partials/chat/suggestions.html`; the M1 set, chosen 2026-10-11, keeps to what the chat answers now, so the mock's "Plan next week" (M4) and "Find a route" (M5) chips are left out; chips keep the mock's 36 px look with an invisible 44 px tap target (ADR-0019); the desktop mock has neither, so desktop reuses the mobile markup), #147 ("New conversation", F16: `GET /c/new` is the empty chat; the control sits in the mobile header's right slot and beside the desktop "Chat" title), #154 (the pending draft card, F2: the mock's state 8 in `partials/chat/draft_card.html`, with each sport's body in `partials/chat/draft_<sport>.html`; the mock shows one route, so several activities render one row each, owner-chosen 2026-10-13; Confirm and Discard are wired by #155 and #156, Edit is shown disabled, and the date chip has no pencil, until #158 wires them; the desktop mock has no draft frame, so desktop reuses the mobile markup), #155 (Confirm, F2: the card collapses to the mock's state 9 row in `partials/chat/draft_confirmed.html`, with each sport's "what was logged" in `partials/chat/logged_<sport>.html`; for climbing, one activity is named as on the mock ("Hades 8a", else "3 × 6A") and several are their total count ("5 climbs"), owner-chosen 2026-10-13; Undo is wired by #157; the mock's follow-up coaching reply is not part of #155), #156 (Discard, F2: the mock has no discarded frame, so the card collapses to a one-line row in the state 9 style, a muted `circle-x` and "Draft discarded" with no action, in `partials/chat/draft_discarded.html`; it stays after a reload, so `list_shown` returns discarded drafts too; owner-approved 2026-10-13 at the plan gate; desktop reuses the mobile markup), #157 (Undo, B4: the state 9 row's Undo asks once with the browser's confirm prompt, then the row collapses to a one-line "Removed" row in `partials/chat/draft_removed.html`, a muted `circle-x` with no action; the mock has no removed frame, owner-approved 2026-10-13 at the plan gate; it stays after a reload, since a confirmed draft with no session was undone; desktop reuses the mobile markup), #158 (Edit, F2 and ADR-0014 invariant 4: the state 8 card's Edit and its date chip, which now carries the mock's pencil, swap the card for an inline form in `partials/chat/draft_edit.html`, with each sport's fields in `partials/chat/edit_<sport>.html`; the mocks have no edit frame, so the layout was owner-approved 2026-10-13 at the plan gate: the card's glass shell and header, a native date input, one row per activity (count, grade as typed, style), duration and RPE side by side, feeling, then Save and Cancel; inputs are 44 px tall, and a rejected value is marked inline under its field from the server, with the browser's own validation off; the chip keeps the mock's 32 px look with an invisible 44 px tap target; desktop reuses the mobile markup), #165 (session continuity, B16: a draft that came within the merge window of a same-sport session shows a muted note line with Lucide `layers`, "Add to the session at HH:MM", above the unchanged Confirm / Edit / Discard in `partials/chat/draft_card.html`, and once confirmed its row reads "Added … to the session at HH:MM" in `partials/chat/draft_confirmed.html`; Undo on that row asks "Remove this from the session?"; the mocks have no frame for it, so the note line was owner-approved 2026-10-13 at the plan gate; desktop reuses the mobile markup); #162 (the choice card, F9, B23: the Components Chat mock's single-mode card, answered row and "Let me type instead", in `components/choice_card.html`; a sport option shows its name and hint from `components/sport_copy.html`, with the mock's chevron; the mock has no frame for a skipped question, so it collapses to a dashed one-line row in the discarded draft's style, "Skipped, you'll type it instead", confirmed at the acceptance gate; the multi-select rows are not built yet; desktop reuses the mobile markup); #163 (the draft card's sport chip, B23: the mocks have no frame for it, so this was owner-chosen 2026-10-21 at the plan gate: for an athlete with more than one sport, the state 8 header's sport label becomes an outline chip with Lucide `chevron-down`, styled like the date chip (32 px look, invisible 44 px tap target), opening a daisyUI `<details>` dropdown of the athlete's other loggable sports, each with its registry icon (the menu is `glass-thick` on an opaque `base-100` fill, because glass nested in the glass card left the card's text legible through it), in `partials/chat/draft_card.html`; a pick swaps in the re-extracted card, with a spinner beside the header while it runs, and a failure keeps the card with an error line to pick again; a one-sport athlete keeps the plain header; desktop reuses the mobile markup); #166 (the same-or-new question, B16: the mocks have no frame for it, so this was owner-chosen 2026-10-21 at the plan gate: inside the state 8 draft card, in place of #165's note line, the question line and the choice card's rows, "Add to the session at HH:MM" and "New session", each with a hint and the mock's chevron, through `choice_options` in `components/choice_card.html`; Confirm is disabled until a tap, which swaps in the settled card, with #165's note line after "Add"; no "Let me type instead"; desktop reuses the mobile markup) |
| Onboarding / Settings | [Onboarding](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Onboarding.dc.html), [Settings](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Settings.dc.html) | `pages/onboarding/`, `pages/settings/` | M0 (appearance, account deletion), M1 (onboarding, the rest of Settings) | #41 (appearance), #17 (account deletion), #74 (onboarding: sports step), #75 (onboarding: availability step), #76 (onboarding: goals step), #97 (onboarding: design goals cards), #98 (onboarding: drop-a-sport dialog on step 1, reused by Settings), #99 (onboarding: goals per sport, step 3), #100 (onboarding: drop-a-sport dialog, built), #105 (onboarding: availability step as day chips and a slider), #77 (onboarding: timezone, step 4; the mock's grade-scale control is left out until M5) |
| Onboarding (AI chats, M1o) | [Onboarding v4](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Onboarding+v4.dc.html) (design system; Liquid Glass v4, loads `glass-v2.css`; replaces the Screens project's `Onboarding` file for onboarding; owner-chosen deviations 2026-10-24, ADR-0028: no per-step AI reply, so Send moves straight to the next card (Send = Continue) and the hint chips stay static; the sport card's subtitle says only the sport is required and the rest is optional, and About you adds an "Optional" line; the summary's "fill them in later from Settings" becomes "Empty fields are fine" until a Settings profile page exists (post-POC); the summary's inputs are typed (number fields, day chips, a timezone select) in the mock's `lg-field` look) | `pages/onboarding/`, `partials/onboarding/`, `components/onboarding_frame.html` | M1o | #254 (sport card, About you, the summary with a plain skeleton; harvests the Liquid Glass v4 set into `glass.css`), #255 (goals, level, schedule), #256 (another sport), #257 (the glass sheet) |
| Conversation list (F17) | [Conversation list v3](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Conversation+list+v3.dc.html) (Liquid Glass v4, option A "content sheet", owner-chosen 2026-10-24 at #212's acceptance gate from the options in [Conversation list v2](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Conversation+list+v2.dc.html), which also shows each v4 class; the first file, `Conversation list`, used fills on glass and is kept as history. On a phone, a `mat-content` sheet over the dimmed chat, opened from a `messages-square` button beside "New conversation" in the header's right slot; on desktop the whole sidebar becomes `mat-content` and widens from `w-60` to `w-72`, with a "Conversations" section under the sections. In both, "New conversation" is the `lg lg-tinted` action at the top of the list (on desktop it leaves the chat header, owner-approved 2026-10-24 at #212's acceptance gate); the open conversation and the active section are `lg lg-clear` capsules with `aria-current`; the other rows are plain 44 px rows, newest first, a one-line truncated title and the last-activity time; an empty state on both. The phone header's menu, list and "New conversation" buttons are `lg lg-clear` icon buttons; restyling the live ones is part of the build ticket) | `partials/chat/conversations.html` | M1a | #212 |
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

Liquid Glass v4, for every new element (ADR-0019, 2026-10-24). Defined in the design system's [`glass-v2.css`](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=glass-v2.css) (copied into the screens project), with the rules in [`LIQUID-GLASS-V3.md`](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=LIQUID-GLASS-V3.md); the reference is the v4 column of [Draft Card v2](https://claude.ai/design/p/d1ddc556-11d3-4434-8e02-9fee28f2efbb?file=Draft+Card+v2.dc.html). Harvested into `glass.css` by the first web ticket that uses them.

| Utility | Recipe | Use |
|---|---|---|
| `mat-content` | `base-100` at 62–72%, `blur(30px) saturate(180%)`, no border, inset top rim and a two-stage shadow | Content cards (draft, choice, route) |
| `lbl-1` / `lbl-2` / `lbl-3` | `base-content` at 95 / 64 / 45% | Labels on a material; `lbl-3` only for non-essential text |
| `lg` | translucent `--lg-tint` body, 1px conic rim (bright top-left, weaker bottom-right), inner edge thickening, pointer-following light, press grows to 1.04 | Base of every control |
| `lg-tinted` / `lg-clear` / `lg-quiet` | primary-tinted glass with a tinted cast / neutral frosted / no material at rest, error-tinted on hover | Primary action / secondary, chips, icon buttons / tertiary or destructive |
| `lg-chip` | 32 px, 13 px text | Chips (with an invisible 44 px tap target) |

A list or sheet the athlete reads is content, so it is `mat-content` and its selected item and main action are `lg` capsules (the conversation list). No glass on glass: `lg` sits on `mat-content` or the wallpaper only; what stays on glass (composer, dock) uses fills (`bg-base-content/10`). The [Conversation list v2](https://claude.ai/design/p/a68b0219-f02b-4a26-8f38-99410521ad2d?file=Conversation+list+v2.dc.html) sampler shows each class in both themes.

Tint is `base-100` in light. In dark it is `base-100` lifted 7% toward `base-content`. Under `prefers-reduced-transparency: reduce` or `prefers-contrast: more`, all glass turns solid and the wallpaper goes flat.

Measured contrast, over the brightest wallpaper spot:
- Body text on glass: dark 10.0–12.4:1, light 11.7–14.4:1.
- Muted (70%) text: at least 5.3:1.
- Text on primary: 8.0:1 in dark, 5.9:1 in light.
- Accent-colored text belongs on glass only, never on the bare wallpaper.
- Warning is never used as text in light (1.8:1).
