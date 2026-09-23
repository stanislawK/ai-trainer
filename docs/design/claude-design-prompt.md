# Claude Design prompt

The brief for both Claude Design projects (ADR-0019). Paste it into Claude Design, or hand it to an agent that drives the claude-design MCP. Keep it in step with ADR-0019: a change to the look, the themes or the component list changes both.

---

**Project: "ai-trainer": Liquid Glass design system and screens**

**Product.** ai-trainer is a chat-first AI training companion for amateur athletes who climb, lift in the gym and cycle. Users log sessions in plain language, ask questions and get cited answers, see statistics and training plans per sport, and resolve ambiguity by tapping concrete options rather than typing. It is used mostly on a phone, often at the crag or in the gym: one-handed, glanceable, and sometimes in bright light. The tone is calm, supportive and competent, never guilt-inducing.

**Aesthetic: Apple macOS/iOS "Liquid Glass" is the gold standard.** Think of the macOS Tahoe Dock: a dark, translucent, rounded pill floating over a softly visible wallpaper. It uses frosted blur with raised saturation, a thin bright specular rim on the top edge, a faint inner shadow, and monochrome glyph icons in rounded-square tiles. It is restrained, precise and quiet. **Dark theme is the default and the hero; the light theme is its equal twin, not an afterthought.**
- Glass sits on a CSS-only wallpaper: very low-chroma, blurred tonal fields in the accent hue over a near-black (dark) or near-white (light) base. No images and no loud gradients.
- Use exactly **one accent color** (a calm macOS-like blue or teal; propose 3 hues and I'll pick). Everything else is neutral with subtly tinted whites and blacks. Sports are distinguished by icon and label, never by color.
- Materials: define three glass levels, **clear** (overlays on content), **regular** (cards, bubbles) and **thick** (dock, composer, modals). Each has its own blur, saturation, tint alpha, rim highlight and shadow. Nest glass at most two levels deep.
- Progressive enhancement: the baseline is `backdrop-filter: blur() saturate()` plus a rim highlight and inner shadow. On Chromium only, add an SVG displacement "refraction" to the dock and composer. Under `@media (prefers-reduced-transparency: reduce)` and `(prefers-contrast: more)`, fall back to solid tinted surfaces. Respect `prefers-reduced-motion`.
- Typography: the system stack only (`-apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", system-ui, sans-serif`; `ui-monospace` for numbers in tables). Use tabular figures for stats. No web fonts.
- Icons: Lucide (stroke 1.75, 20/24px), always monochrome.
- Accessibility: text contrast of at least 4.5:1 on glass in both themes (test against the brightest wallpaper spot), tap targets of at least 44px, visible focus rings, full keyboard support.
- Motion: short spring-like transitions (150–250ms). Glass surfaces fade and scale in slightly. No infinite decorative loops except the thinking indicator.

**Technical contract (important, because the mocks become production code).**
- Production stack: server-rendered Jinja2, **htmx 4**, **daisyUI 5** on **Tailwind CSS 4**. No React or SPA in production. Every mock must use **real daisyUI 5 component classes** (`btn`, `card`, `input`, `join`, `dock`, `menu`, `tabs`, `table`, `badge`, `modal`, `skeleton`, `toggle`, `radio`, `checkbox`, `chat`, `stat`, `steps`, `calendar`-like grids, `dropdown`, `toast`, `alert`, `loading`) and Tailwind utilities, so the markup can be copied into Jinja almost verbatim.
- In each `.dc.html` `<helmet>`, load pinned `@tailwindcss/browser@4.3.3` and `daisyui@5.7.43` from jsdelivr, plus `./theme.css`.
- **`theme.css` is the single handoff file.** It defines two daisyUI themes as CSS variable blocks, `[data-theme="trainer-dark"]` and `[data-theme="trainer-light"]`, using daisyUI's exact variable names (`--color-base-100/200/300`, `--color-base-content`, `--color-primary`/`-content`, `neutral`, `info`, `success`, `warning`, `error` and their `-content`, `--radius-selector/field/box`, `--size-selector/field`, `--border`, `--depth`, `--noise`, `color-scheme`). All colors are in **oklch**. After the themes it defines the glass layer as a small set of utility classes (`glass-clear`, `glass-regular`, `glass-thick`, `glass-rim`, `wallpaper`) built only from those variables. There are no other custom classes; any one-off styling is Tailwind utilities in the markup.
- Every mock has a visible **theme switch** that toggles `data-theme` on `<html>`, so I can review both themes.
- Name frames with `data-screen-label`. Show mobile frames at **390×844** first, and desktop at **1440×900** where the layout differs.
- Keep files under about 1000 lines and split by area.

**Deliverables: project 1, the design system**
1. `theme.css` (above).
2. `Foundations.dc.html`:
   - palette swatches for both themes with contrast ratios
   - the three glass materials over the wallpaper, in both themes
   - type scale, radii, spacing, elevation and motion tokens
   - icon sampler.
3. Components, split by area into `Components Core`, `Components Navigation`, `Components Chat` and `Components Loading` (`.dc.html`), every component in dark and light, with all states (default, hover, focus, active, disabled, loading):
   - Buttons (primary, ghost, glass), inputs, select, toggle, segmented control (`join`/`tabs`), badges, alert/toast, modal (including a destructive-confirm variant), table, stat tile.
   - Navigation: on mobile, a floating **glass dock** at the bottom (Chat, Plans, Stats, History, Settings; Admin only for admins), safe-area aware. On desktop, a slim glass sidebar. Explore 3 options for how the dock coexists with the chat composer (e.g. the dock collapses to a mini-pill while typing, the composer docks above the dock, or the dock hides on the chat screen behind a menu button).
   - **Chat**:
     - User messages are right-aligned, accent-tinted regular glass.
     - Assistant messages have no bubble: full-width readable text (max ~65ch) with a small avatar mark.
     - Timestamps, grouped consecutive messages, a streaming caret, an error bubble with Retry, and a "jump to latest" pill.
   - **Composer**: a thick-glass floating pill with an auto-growing textarea, a send button (disabled when empty, a stop button while streaming) and a keyboard hint on desktop.
   - **Autocomplete / suggestions**:
     - As the user types, a glass popover rises above the composer. It shows up to 6 rows, each with an icon, the label with the matched substring highlighted, a short detail and an optional category header.
     - Arrow keys, Enter/Tab to accept and Esc to dismiss on desktop; a tap on mobile.
     - On an empty composer, show 3–4 starter suggestion chips instead.
     - Sample: typing "lo" shows "Log a boulder session" and "Log yesterday's ride" (sample data only; wearable sync is a PRD non-goal).
   - **Choice card**, single and multi mode:
     - A question in the assistant's voice, then 2–5 large tappable option rows (label, distinguishing detail such as crag, date or grade, optional badge).
     - Single mode submits on tap. Multi mode uses checkbox rows and a sticky "Confirm (2)" button.
     - Always ends with a quiet "Let me type instead" link.
     - Answered state: collapses to show the chosen option.
   - **Draft card** for a logged session:
     - Structured fields per sport (climbing: route, grade, style, attempts; gym: exercise × sets × reps × load; cycling: distance, time, elevation).
     - An editable "assumed date" chip, and Confirm / Edit / Discard actions.
     - Confirmed and discarded end states.
   - **Cited answer**: inline numbered citations, a sources row of glass pills (title and domain), and a "web" label on web results.
   - **Route card** and **recommendation list item**, each showing its source and fetch date.
   - **Thinking indicator**:
     - A small glass orb with a slow liquid shimmer and a word in shimmering gradient text that changes every ~2.5s with a soft vertical "bump" transition.
     - It cycles sport-flavored words ("Chalking up…", "Clipping in…", "Warming up…", "Spotting…", "Spinning up…", "Reading the route…", "Counting reps…") until a real phase arrives ("Checking your history…", "Looking up the route on 8a.nu…"), which replaces the word.
     - After ~8s, add a subtle elapsed-time hint so it never looks frozen.
     - Reduced-motion variant: a static orb plus a text change only.
   - **Skeletons**: one for every async element, **matching the real element's exact size and layout**: assistant message (3 lines), choice card, draft card, stat tiles row, chart, table rows, calendar month, plan week. Use a gentle glass shimmer.
4. `Favicon.dc.html`:
   - 3 options for a simple app mark in the same language: a monochrome glyph on a rounded-square (squircle) glass tile, e.g. an ascending line that reads as both a summit and a progress chart.
   - Show it at 16, 32, 180 (apple-touch), 192 and 512 (maskable, with the safe zone shown), in dark and light.
   - It must stay legible at 16px.

**Deliverables: project 2, the screens** (use the design system; one canvas file per area; mobile first, desktop where it differs; both themes for at least the chat and sign-in screens):
1. **Sign-in**: a centered glass card on the wallpaper with the mark, name, a one-line value statement, a "Continue with Google" button (the only sign-in method) and a small note that new accounts are approved by an admin.
2. **Status and errors**, one family:
   - pending approval and disabled account (neutral, reveals nothing about other accounts, sign-out only)
   - 404 (friendly, sport-flavored line, "Back to chat")
   - 500
   - 401 (links to sign-in)
   - 403 "session needs a refresh".
3. **Onboarding**: a 3–4 step flow (sports, weekly availability as days and minutes, goals, timezone and grade scale) with a progress indicator. It lands in the chat.
4. **Chat (main screen, the most important one).** States:
   - empty, with a greeting and starter chips
   - a conversation mixing text, a cited answer and a route card
   - thinking
   - streaming
   - choice card (single and multi)
   - autocomplete open
   - draft card pending, then confirmed
   - initial-load skeleton
   - a network error with retry.

   Show where the dock goes while typing, and the keyboard-open state on mobile.
5. **Statistics**:
   - a per-sport segmented control (Climbing / Gym / Cycling / All) and a date range
   - stat tiles
   - charts: weekly volume bars, load trend line, climbing grade pyramid, cycling distance and elevation
   - a recent-sessions table (it becomes a card list on mobile)
   - skeleton and empty states.

   Charts will be Chart.js themed from the CSS variables, so design them as clean, gridline-light glass panels.
6. **Plans**: per-sport tabs, this week as a day-by-day list (planned vs done), the long-term phases as a horizontal timeline, and a "proposed change" banner (AI proposals are drafts to confirm or dismiss).
7. **History**: a month calendar with a sport glyph per day, and tapping a day shows that day's sessions. Plus a list view and session detail.
8. **Settings**: theme (Dark / Light segmented), sports, availability, goals, timezone, grade scale, sign out, and a **danger zone** to delete the account with a typed-confirmation modal.
9. **Admin: users**: a table with email, name, status badge (pending / active / disabled), joined date and a status control. The admin's own row is locked. On mobile it becomes a card list. Plus the confirmation toast.

**Process.** Start with `Foundations` and 3 accent-hue options plus the 3 dock/composer options, and show them to me before building everything else. Keep option IDs stable so I can refer to them. Use sample data that looks realistic (e.g. "Hades 8a, Frankenjura, 3 tries") but no filler. I will make the final visual tweaks myself in the Claude Design editor, so keep the markup canonical and editable.
