---
name: implement-design
description: Turn a Claude Design mock into daisyUI/Jinja templates and prove parity with Playwright — harvest tokens and markup through the claude-design MCP, implement, then compare mock and app side by side at mobile and desktop sizes in both themes.
argument-hint: "[screen name or Claude Design file path]"
---

# Implement design: $ARGUMENTS (ADR-0019)

Runs inside a web ticket (`/apply-ticket`), between tests-first and the review gate. It reads Claude Design and never writes to it. Visual changes to a design are the owner's job in the Claude Design editor, or a separate design session.

1. **Locate.** Find the screen in `docs/design/README.md`: its project, its file and its target template. If the ticket names no design file, or the file doesn't exist (`mcp__claude-design__list_files`), stop and ask. Don't design in code.
2. **Harvest the tokens.**
   - `mcp__claude-design__read_file` the design system's `theme.css`. The body is entity-escaped, so decode `&amp; &lt; &gt;`.
   - Also read the screen file. Any `<style id="__om-edit-overrides">` rule there is an owner tweak. When it changes a token value, it belongs in the theme; when it changes one element, it belongs in that element's utilities. List every override in the plan and never drop one silently.
   - Map each `[data-theme="trainer-*"]` block to an `@plugin "daisyui/theme" { name; default; prefersdark; color-scheme; --color-* … }` block in `src/ai_trainer/web/static/css/theme.css`, keeping the variable names unchanged. Map the glass classes to `@utility` blocks in `static/css/glass.css`.
   - Diff the result against the repo's current files. Token changes that the ticket didn't ask for go into the plan, not in silently.
3. **Harvest the markup.**
   - Copy the frame's daisyUI markup into its target: `layouts/`, `pages/<feature>/`, `partials/<feature>/`, or a macro in `components/`.
   - Replace sample data with context variables and move every user-facing string into the template (G5).
   - Add the htmx 4 attributes (`.claude/rules/web.md`).
   - Drop editor-only attributes: `data-dc-*`, `data-screen-label`, `data-comment-anchor`, `{{ }}` DC holes, `sc-for` and `sc-if`. Keep the classes.
   - Replace inline SVGs with the `icon()` macro.
4. **Build.** Run `make css`, then start the app (`docker compose up -d`) and seed a session for gated pages with `uv run python scripts/dev_session.py <email>`.
5. **Parity check with Playwright MCP.** For each viewport (390×844, then 1440×900) and each theme (`trainer-dark`, `trainer-light`):
   - **Mock.** Get a fresh `serve_url` from `mcp__claude-design__render_preview` (it expires quickly, and must never appear in chat, commits or files). Then:
     - `mcp__playwright__browser_navigate` to it
     - `browser_resize`
     - set the theme with `browser_evaluate` (`document.documentElement.dataset.theme = '…'`)
     - `browser_take_screenshot` with an explicit `filename` under the scratchpad directory (e.g. `<scratchpad>/parity/<screen>-mock-<viewport>-<theme>.png`) — not the tool's default ephemeral output path, so the file survives past this step for the human to look at.
   - **App.** Add the session cookie with `browser_run_code_unsafe` (`context.addCookies`), then navigate to `http://localhost:8000/<route>`, set the theme the same way, and take a screenshot the same way (`<scratchpad>/parity/<screen>-app-<viewport>-<theme>.png`).
   - **Probe.** Run `browser_evaluate` on both pages and compare:
     - `getComputedStyle` of the key surfaces: background, `backdrop-filter`, border radius, font family and size
     - `document.documentElement.scrollWidth <= innerWidth` (no horizontal overflow)
     - that interactive targets are at least 44 px tall.
   - **Gate.** Check `browser_console_messages` and `browser_network_requests`: no errors, no 404s, and no request leaves localhost.
   - The screenshots are the ground truth, and the probes explain any difference. Fix what differs and repeat. After three rounds that don't converge on the same element, measure the element and its parent, state the root cause in one sentence, and make one decisive edit.
6. **Human gate (parity).** This check is never self-certified (ADR-0019). Once every viewport/theme combination looks right, stop and use `AskUserQuestion` (Approve / Request changes) pointing at the saved screenshot folder, with the probe table in the message. Don't fold "the screenshots matched" into the review-gate summary as already-settled — wait for this answer first, and only move on once it comes back Approve. Do not delete the screenshots when done; they stay in the scratchpad for the human to revisit.
7. **Evidence.** For the review gate, reference the same screenshots (mock | app) per viewport and theme, the probe table, and any intended differences with their reasons (e.g. real data is longer than the sample), plus a note that the human gate above passed. Don't commit the screenshots.
8. **Behavior specs.** `tests/e2e/` is expensive and reserved for critical flows (ADR-0013) — only add or extend a pytest-playwright spec here when this screen's flow is one of those; otherwise the parity check above is the verification, and no new spec is needed.
9. **Map.** Fill in the screen's row in `docs/design/README.md` (file, template, ticket) in the same change.

## Anti-patterns

- Hand-writing custom CSS instead of harvesting theme tokens.
- Inventing a design where no mock exists.
- Writing to the Claude Design project from this skill.
- Pasting a `serve_url` anywhere.
- Committing pixel snapshots.
- Loading a CDN, a web font or a remote icon in the app.
- Declaring parity without the human gate, or deleting the screenshots before the human has looked at them.
- Adding a `tests/e2e/` spec for a screen whose flow isn't critical — the parity check already covers it.
