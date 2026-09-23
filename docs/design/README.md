# Design

Claude Design is the visual source of truth, and this folder is the map to it ([ADR-0019](../adr/0019-visual-design-system.md)). The brief for both projects is [claude-design-prompt.md](claude-design-prompt.md).

## Projects

| Project | Purpose | Link |
|---|---|---|
| ai-trainer · Liquid Glass | Design system: `theme.css`, foundations, components, favicon | _added when the project is created_ |
| ai-trainer · Screens | Every screen in mobile and desktop frames, in both themes | _added when the project is created_ |

Share only `claude.ai/design/...` links here. Never paste a `serve_url`: it carries a short-lived token.

## Handoff contract

- `theme.css` in the design-system project defines `[data-theme="trainer-dark"]` and `[data-theme="trainer-light"]` with daisyUI's variable names, plus the glass utilities. In the repo, it becomes `src/ai_trainer/web/static/css/theme.css` (`@plugin "daisyui/theme"` blocks) and `glass.css` (`@utility` blocks).
- Mocks use real daisyUI 5 classes, so their markup moves into `templates/` with sample data replaced by context variables.
- Owner tweaks made in the Claude Design editor land as `__om-edit-overrides` styles. `/implement-design` folds them back into tokens before harvesting.

## Screen → file → ticket

Filled in as screens and tickets are created.

`Ticket` holds only a GitHub issue number, or _tbd_ until the ticket exists. `Milestone` is where the screen is planned (PRD delivery mapping).

| Screen | Claude Design file | Template | Milestone | Ticket |
|---|---|---|---|---|
| Sign-in | _tbd_ | `pages/auth/sign_in.html` | M0 | _tbd_ |
| Status (pending / disabled) | _tbd_ | `pages/auth/status.html` | M0 | _tbd_ |
| Errors (404, 500, 401, 403) | _tbd_ | `pages/errors/<code>.html` | M0 | _tbd_ |
| App shell (dock, sidebar) | _tbd_ | `layouts/app.html` | M0 | _tbd_ |
| Chat | _tbd_ | `pages/chat/index.html` | M1 | _tbd_ |
| Onboarding / Settings | _tbd_ | `pages/onboarding/`, `pages/settings/` | M0 (sign-in, account deletion), M1 (onboarding) | #17 (account deletion) |
| History | _tbd_ | `pages/history/index.html` | M2 | _tbd_ |
| Statistics | _tbd_ | `pages/stats/index.html` | M2 | _tbd_ |
| Plans | _tbd_ | `pages/plans/index.html` | M4 | _tbd_ |
| Admin: users | _tbd_ | `pages/admin/users.html` | M0 | #16 |

## Tokens

The final values are copied here from `theme.css` once the owner approves the design system.
