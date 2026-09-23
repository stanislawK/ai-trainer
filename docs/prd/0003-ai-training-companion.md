# PRD 0003 — AI training companion

| | |
|---|---|
| Product | ai-trainer |
| Version | 0.4 |
| Status | Approved |
| Date | 2026-09-23 |
| Related | ADR-0001 … ADR-0019 ([index](../adr/README.md)); supersedes [PRD 0002](0002-ai-training-companion.md) v0.3 on approval — adds sport inference (B23) and the sport glyph on replies (F15) |

## Problem

Amateur athletes with full-time jobs have 3–8 hours a week to train. They can't afford wasted sessions, yet most plans assume a professional's time and recovery. Many of them train more than one sport and have no single view of cumulative fatigue: a hard board session followed by a heavy back day loads the same fingers and pulling muscles twice. They also won't fill in forms, so logging has to be as easy as sending a message. Existing apps are single-sport, form-heavy, or generic chatbots that know neither the athlete's history nor trustworthy training science.

## Goals

- Logging a session is as easy as typing one sentence.
- Every athlete gets one cross-sport view of what they did and how loaded they are.
- Training questions get expert, cited answers.
- Plans fit the time the athlete actually has, and adapt when life gets in the way.
- The tone is supportive and empathetic, never guilt-tripping.
- New sports can be added without reworking the product.

## Non-goals

- Polish or any language other than English (v1).
- Strava, Garmin or other wearable sync.
- Native mobile app.
- Coach or team features.
- Nutrition.
- Payments.
- Medical advice or diagnosis.
- Login methods other than Google (POC).
- Hosted deployment: the POC runs locally via docker compose.

## Personas

- **Kasia — climber-first cross-trainer.** 34, software engineer. Climbs twice a week, goes to the gym once, commutes by bike. Goal: first 7a sport route by spring. Pain: can't tell whether the gym pull day is ruining the climbing.
- **Tomek — gran fondo cyclist.** 41, project manager with two kids. 6 hours a week, one fixed long ride on Sunday. Goal: finish a 200 km gran fondo in June. Pain: weekday sessions get cancelled and the plan falls apart.
- **Ola — gym-focused climber.** 28, nurse on shifts. Builds strength in the gym to support bouldering. Goal: a pull-up with +10 kg and a first V5 boulder. Pain: shifts change weekly, and generic plans assume fixed training days.

## Product shape and must-have UX

- A chat-first web app, server-rendered, designed mobile-first for a phone browser and installable to the home screen.
- A calm, glassy look in the spirit of Apple's Liquid Glass. Dark theme by default, with an equal light theme; each browser remembers the athlete's choice.
- The app never looks frozen: every loading region shows a placeholder the size of what will appear, and while the AI works on a reply the chat shows that it is thinking and, when known, what it is doing.
- Logging happens by typing plain text. The app shows a structured draft of what it understood and saves only after the user confirms or edits it.
- Ambiguity is resolved by picking from a short list of concrete options, not by typing the answer again.
- The app guesses the sport instead of asking. The guess is visible and easy to change on the draft card, and a question comes only when the guess really can't be made.
- Every reply shows at a glance which sport it is about, through that sport's glyph.
- The app knows what day it is where the athlete is, so "yesterday" lands on the right date.
- One consistent, supportive tone in every reply. Missed sessions are met with understanding and an adjusted plan, never guilt.
- Answers built on the knowledge base show their sources; web results are labeled as web. Route and crag data from a public database shows that source and when it was fetched.
- Every AI-made change to the user's data (a logged session, a plan change) is a draft or proposal the user confirms.
- Visual design comes from Claude Design and maps onto daisyUI components.

## Primary user flows

1. **Sign in and onboarding** — Open the sign-in page → continue with Google → a new account is pending, showing only a status screen until an admin activates it → choose sports → set weekly availability (days and minutes) → set one or more goals → land in the chat.
2. **Log a session** — Type "I climbed 3 6A and 2 6B boulders, felt strong" → the app infers the sport → a draft card shows sport, activities, grades and effort → confirm or edit → saved together with the original text. If something is ambiguous, the app offers a short list of options to pick from first.
3. **Ask a question** — "How often should I hangboard?" → an answer grounded in the knowledge base, citing its sources and taking the athlete's recent load into account. Web results, if used, are labeled.
4. **Plan** — Set a goal with a target date → a long-term plan in phases → this week's plan, fitted to the athlete's availability.
5. **Re-plan** — "I missed Tuesday" or "only 45 minutes today" → the app proposes an adjusted week → the user accepts or tweaks it.
6. **Weekly report** — A summary per sport and across sports: volume, load, highlights, goal progress and one suggestion.
7. **Log a named route** — "I sent Zygzag in Jerzmanice" → the app looks the route up in a public database, fills in the grade and the crag and shows where that came from → if the name is used at several crags, the athlete picks the right one → confirm.
8. **Plan a trip** — "I fly to Malaga, recommend me the top 10 routes between 7a and 7b+" → the app finds the crags within range and returns a ranked list: grade, ascents, onsight rate, recommendation and what climbers say about each route.

A single message can mix flows, e.g. a log and a question (B14).

## Functional requirements

IDs are stable across revisions. Never reuse a retired ID.

### General (G)

- **G1** — Per-user data isolation: a user can only ever see and change their own data.
- **G2** — An empathetic, supportive persona, consistent across every reply.
- **G3** — No medical diagnosis. A mention of injury or pain is handled before anything else in the message: acknowledge it, avoid advice that could aggravate it, and recommend a qualified professional.
- **G4** — A new sport is added as a plugin, with no change to core logic and no schema migration.
- **G5** — English only in v1 (UI, input and replies). The design must not block adding Polish later.
- **G6** — Every AI capability ships with an eval dataset and meets its quality bar before release.
- **G7** — A user can delete their account and all their data.
- **G8** — Access is approval-gated: a new account is pending and cannot use the app until an admin activates it. An admin can disable an account again.
- **G9** — The app knows the athlete's local date and time. Relative expressions resolve against the athlete's timezone, never the server's.
- **G10** — Data fetched from a public source is shown with that source and the date it was fetched, and is never presented as the app's own knowledge.
- **G11** — Mobile-first and installable: every page works at a 390 px wide viewport without horizontal scrolling, with tap targets of at least 44 px, and the app can be added to a phone's home screen with its own icon. Dark theme is the default, a light theme is available, and the choice is remembered in the browser.

### Backend (B)

- **B1** — Plain text becomes a structured session draft; one message may contain several activities and several sports.
- **B2** — The original text is always stored with the parsed record.
- **B3** — When input is ambiguous, the app asks a clarifying question instead of guessing.
- **B4** — Sessions can be created, read, updated and deleted.
- **B5** — Training-load metrics are computed across sports in a common vocabulary.
- **B6** — Reports: weekly, per sport, and goal progress.
- **B7** — Knowledge-base documents are ingested by an admin-run pipeline.
- **B8** — Answers grounded in the knowledge base cite their sources; web search results are labeled as such.
- **B9** — Goals with a target date, and a long-term plan in phases.
- **B10** — A weekly plan that fits the athlete's availability.
- **B11** — Re-planning after missed or changed sessions; previous plan versions are kept.
- **B12** — Cross-training conflict awareness: overlapping load on the same body systems (e.g. fingers, upper-body pulling) is flagged.
- **B13** — Chat history per user.
- **B14** — A message containing several requests (e.g. a log and a question) gets each of them handled.
- **B15** — Relative dates and times ("yesterday", "Tuesday", "this morning") resolve to a concrete date in the athlete's timezone.
- **B16** — Session continuity: logs sent close together join one session; further apart, the app asks whether it was the same session.
- **B17** — Ambiguity is resolved by picking from a short list of concrete options, single- or multi-select, without typing. Refines B3.
- **B18** — A named route in a message is looked up against a public source to fill in its grade and crag; a name used at several crags becomes a choice (B17).
- **B19** — Route, crag and place data is cached as shared reference data with its source and fetch date, and is never user-scoped.
- **B20** — Grades are stored as the original string, its scale, the canonical French equivalent and an ordinal; the display scale is a profile preference defaulting to French.
- **B21** — Given a place and a grade range, the app finds the crags within range and returns a ranked route list in one fixed shape: grade, ascent count, onsight rate, recommendation and a short summary of comments.
- **B22** — External lookups are budgeted and rate-limited per user; a failed, empty or low-confidence lookup asks the athlete instead of inventing data.
- **B23** — Sport inference: the sport of each logged activity is inferred from the message and its context: wording, units and grade scales ("6A", "km", "5×5 at 100 kg"), the athlete's own sports (an athlete with one sport needs no guess), the current conversation and the most recent sessions. A confident inference is not asked about; the draft card shows it and the athlete can change it (F2). Only when two or more of the athlete's sports stay plausible does the app ask, with a choice card offering just those sports (B17). Refines B3 for the sport.

### Frontend (F)

- **F1** — Chat with streamed replies.
- **F2** — A draft-confirm card for logged sessions (confirm / edit / discard).
- **F3** — A history / calendar view of sessions.
- **F4** — A plan view per sport: this week and the long-term phases.
- **F5** — A report view.
- **F6** — Onboarding and profile: sports, availability, goals, timezone, display grade scale, and account deletion (G7).
- **F7** — Admin view: list users with their status and change a user's status (pending / active / disabled).
- **F8** — A signed-in user whose access is pending or disabled sees a neutral status screen and nothing else.
- **F9** — A choice card: 2–N concrete options, single- or multi-select, resolved by tapping, with a "let me type instead" escape.
- **F10** — A route card and a recommendation list, each showing the source and the fetch date.
- **F11** — A sign-in page with a "Continue with Google" button, and designed pages for not found (404), server error (500), signed out (401) and an expired form session (403). The unauthenticated page links to sign-in.
- **F12** — A statistics view per sport and across sports: charts and tables of volume, load and progress over a chosen date range.
- **F13** — Composer suggestions: while the athlete types, the chat offers matching suggestions to pick by tap or keyboard. What is suggested is decided per milestone; the pattern exists from the first chat release.
- **F14** — A thinking indicator: from sending a message until the reply starts streaming, the chat shows that the AI is working, naming the current step when the server reports one.
- **F15** — Sport glyph on replies: each assistant reply carries the glyph of the sport it is about, the same glyph that sport uses everywhere else (statistics, history, draft card). A reply about several sports or about none (a general question, a cross-sport report) shows the app mark.

## Configuration and contracts

- All settings come from the environment / `.env` through pydantic-settings. `.env.example` is committed; `.env` never is. Indicative keys (the Settings class is the source of truth): OpenRouter API key; model ID per prompt template (router, extraction, specialists); embedding model and dimension; eval judge model; database URL; session secret; Google OAuth client ID and secret; the admin email list (`ADMIN_EMAILS`); the session merge window (`session_merge_window_min`); the clarification option cap (`max_clarification_options`); the reference-data lifetime (`reference_data_ttl_days`); the per-user external lookup budget (`external_lookups_per_user_per_day`); the default search radius (`default_search_radius_km`).
- HTTP contract: an OpenAPI snapshot at `docs/api/openapi.json`, generated by a script and never hand-edited (created in M0).
- Prompt templates and eval datasets are versioned in the repository (ADR-0008, ADR-0009).

## Acceptance criteria

- [ ] A new user signs in with Google, completes onboarding and lands in the chat.
- [ ] "I climbed 3 6A and 2 6B boulders" produces a climbing draft with 3 × 6A and 2 × 6B on the Fontainebleau scale; after confirmation it is saved with the original text.
- [ ] A message mixing climbing and cycling produces one draft per sport in a single reply.
- [ ] An ambiguous log triggers a clarifying question, not a guess.
- [ ] A message mentioning pain gets the safety response before any other part of the message is handled.
- [ ] A training question gets an answer with at least one knowledge-base citation; web results are labeled as web.
- [ ] Setting a goal produces a long-term plan and a week plan within the stated availability.
- [ ] Reporting a missed session produces an adjusted week proposal, and the previous plan version is kept.
- [ ] The weekly report shows per-sport and cross-sport load and flags overlapping finger / pulling load when present.
- [ ] User A cannot read or change user B's data through any page, endpoint or tool.
- [ ] After a user deletes their account, none of their data remains.
- [ ] A brand-new Google sign-in creates a pending account that reaches no feature at all.
- [ ] After an admin activates that account, the user can use the app.
- [ ] Disabling an account ends its existing sessions immediately, not when they expire.
- [ ] A signed-in non-admin cannot open the admin view or change any user's status.
- [ ] Every AI capability has an eval dataset and a committed baseline.
- [ ] "I did 3 6b+ yesterday", sent on 2026-09-16 by an athlete in `Europe/Warsaw`, saves three activities dated 2026-09-15.
- [ ] Three logs sent minutes apart become one session; the same three a day apart produce a choice card offering same session or separate sessions.
- [ ] "I sent Zygzag in Jerzmanice" produces a climbing draft naming the route, the crag and grade 7c, attributed to its source with a fetch date.
- [ ] A route name found at three crags produces a choice card with three distinguishable options, resolved without typing.
- [ ] "I fly to Malaga, recommend me the top 10 routes between 7a and 7b+" returns ten routes within the configured radius, each with grade, ascent count, onsight rate and a one-line summary of comments.
- [ ] A failed external lookup asks the athlete for the grade and never invents one.
- [ ] A grade typed as "V5" is stored with its original string and its French equivalent, and displays in the athlete's chosen scale.
- [ ] A first visit renders in the dark theme; after switching to light and reloading, the page renders light with no flash of the dark theme.
- [ ] Every page renders at 390 × 844 without horizontal scrolling, and the app can be added to a phone's home screen with its icon.
- [ ] An unknown URL shows the designed not-found page with a way back to the chat.
- [ ] After sending a message, the thinking indicator is visible until the first streamed token arrives.
- [ ] "Did 4×4s on the board" produces a climbing draft without a question about the sport.
- [ ] "2 hours easy" from an athlete whose only sport is cycling produces a cycling draft.
- [ ] "Did intervals" from an athlete who does gym and cycling, with no recent context that settles it, produces a choice card offering exactly gym and cycling.
- [ ] A reply about a logged ride shows the cycling glyph; a reply to "How was my week?" across sports shows the app mark.

## Success metrics

- Router intent accuracy, sport-inference accuracy and extraction field accuracy on the eval datasets; thresholds are set from the first M1 baseline.
- Share of logs that needed a sport question (lower is better, without lowering sport-inference accuracy).
- Share of logged sessions saved without edits on the draft card.
- Plan adherence: planned sessions completed per week.
- Weekly active loggers.

## Out of scope / later

- Polish UI and replies. The seams exist: `User.locale`, a locale in prompt template file names, and a `language` field on knowledge-base chunks.
- More sports (running, swimming, …) through the plugin model.
- Wearable / Strava import.
- Hosted deployment and other login methods.
- An internal human-review page for evals.
- A richer admin view: search, pagination, and a screen for the status-change history. M0 ships a minimal list with a status control.
- Offline use: a service worker, cached pages or queued messages. v1 is installable but needs a connection.
- Syncing the theme choice across devices.
- Exposing prompt templates as MCP prompts to external clients.
- Project tracking: matching a logged `attempt` on a named route to a later `redpoint`, and counting that as goal progress (B9).
- Route lookup for sports other than climbing (cycling segments, gym exercise databases). The `ExternalRouteSource` port (ADR-0016) is shaped so this is an adapter, not a redesign.

## Delivery mapping

| Milestone | Scope | Requirements |
|---|---|---|
| M0 Foundations | Project skeleton, docker compose, CI, Google sign-in with approval gating, a minimal admin user list, LLM gateway, prompt registry, eval harness | G1, G6, G7, G8, F6 (sign-in), F7, F8 |
| M0 Design foundation | Liquid Glass theme, dark default with a remembered toggle, app shell, icons and manifest, sign-in and error pages, first end-to-end specs | G11, F11 |
| M1 Log it | Router + `log_session` path; session envelope, `SportRegistry` and the climbing, gym and cycling plugins together; sport inference; draft → confirm; time awareness and session continuity; structured clarification and the choice card; composer suggestions, the thinking indicator and the sport glyph on replies; eval datasets for the router and all three extraction templates | B1–B4, B13–B17, B23, F1, F2, F9, F13–F15, G2–G5, G9 |
| M2 See it | History, cross-sport load, weekly report, statistics per sport | B5, B6, B12, F3, F5, F12 |
| M3 Know it | Ingestion-pipeline ADR → ingestion → cited Q&A | B7, B8 |
| M4 Plan it | Goals, long-term and weekly plans, re-planning | B9–B11, F4 |
| M5 Explore it | Grade conversion and route identity, the 8a.nu route lookup, crag geography, area recommendations | B18–B22, F10, G10 |

G7 is delivered in M0 and must stay complete in every later milestone: each new user-owned table joins the account deletion.
