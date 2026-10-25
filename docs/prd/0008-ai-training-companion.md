# PRD 0008 — AI training companion

| | |
|---|---|
| Product | ai-trainer |
| Version | 0.9 |
| Status | Approved |
| Date | 2026-10-25 |
| Related | ADR-0001 … ADR-0028 ([index](../adr/README.md)); supersedes [PRD 0007](0007-ai-training-companion.md) v0.8 — Europe first: metric units and European grade scales are the bar, imperial units and American grades are best effort (G15, owner-directed 2026-10-25). Keeps v0.8's AI onboarding (B33–B36, F20, F21) and v0.7's gyms and location check (B27–B32, F18, F19) |

## Problem

Amateur athletes with full-time jobs have 3–8 hours a week to train. They can't afford wasted sessions, yet most plans assume a professional's time and recovery. Many of them train more than one sport and have no single view of cumulative fatigue: a hard board session followed by a heavy back day loads the same fingers and pulling muscles twice. They also won't fill in forms, so logging has to be as easy as sending a message. Existing apps are single-sport, form-heavy, or generic chatbots that know neither the athlete's history nor trustworthy training science.

## Goals

- Logging a session is as easy as typing one sentence, and as quick.
- Every athlete gets one cross-sport view of what they did and how loaded they are.
- Training questions get expert, cited answers.
- Plans fit the time the athlete actually has, and adapt when life gets in the way.
- The tone is supportive and empathetic, never guilt-tripping.
- New sports can be added without reworking the product.
- Real athletes can use it on their phones, and the history they log is never lost.
- A grade means what the athlete's gym means by it.
- The app speaks the units and grades its European athletes use.

## Non-goals

- Polish or any language other than English (v1).
- Strava, Garmin or other wearable sync.
- Native mobile app, and any location use in the background.
- Full support for imperial units and American grade scales (V, YDS) in v1. They are best effort (G15).
- Coach or team features.
- Nutrition.
- Payments.
- Medical advice or diagnosis.
- Login methods other than Google (POC).
- More than one hosted instance: no staging environment, high availability or multi-region setup. The POC runs as one production instance on the owner's domain (G12).

## Personas

- **Kasia — climber-first cross-trainer.** 34, software engineer. Climbs twice a week, goes to the gym once, commutes by bike. Goal: first 7a sport route by spring. Pain: can't tell whether the gym pull day is ruining the climbing.
- **Tomek — gran fondo cyclist.** 41, project manager with two kids. 6 hours a week, one fixed long ride on Sunday. Goal: finish a 200 km gran fondo in June. Pain: weekday sessions get cancelled and the plan falls apart.
- **Ola — gym-focused climber.** 28, nurse on shifts. Builds strength in the gym to support bouldering. Goal: a pull-up with +10 kg and a first 6C boulder. Pain: shifts change weekly, and generic plans assume fixed training days.

## Product shape and must-have UX

- A chat-first web app, server-rendered, designed mobile-first for a phone browser and installable to the home screen.
- A calm, glassy look in the spirit of Apple's Liquid Glass. Dark theme by default, with an equal light theme; each browser remembers the athlete's choice.
- The app never looks frozen: every loading region shows a placeholder the size of what will appear, and while the AI works on a reply the chat shows that it is thinking and, when known, what it is doing.
- The athlete can start a fresh conversation at any time and come back to a recent one. A fresh conversation doesn't carry the earlier one's context. Conversations are kept for a limited time (B26), while the training data saved from them is kept for good.
- Logging happens by typing plain text. The app shows a structured draft of what it understood and saves only after the user confirms or edits it.
- Ambiguity is resolved by picking from a short list of concrete options, not by typing the answer again.
- The app guesses the sport instead of asking. The guess is visible and easy to change on the draft card, and a question comes only when the guess really can't be made.
- Every reply shows at a glance which sport it is about, through that sport's glyph.
- The app knows what day it is where the athlete is, so "yesterday" lands on the right date.
- One consistent, supportive tone in every reply. Missed sessions are met with understanding and an adjusted plan, never guilt.
- Answers built on the knowledge base show their sources; web results are labeled as web. Route and crag data from a public database shows that source and when it was fetched.
- A gym grade stays the gym's grade. Climbing gyms grade boulders on their own numbers (1–9, 1–10) or colours, never Fontainebleau, and grade routes in French or, in Poland, sometimes the Polish (Kurtyka) scale. The app never turns a gym's "6" into "6A".
- The phone's location is used only if the athlete says yes, only at the moment they send a log, and never saved. Saying no means the app doesn't even try.
- Onboarding is a few short chats, not forms. Only the first sport is required. Everything after it is voluntary, the screens say so, and every step can be skipped. The athlete never waits between steps: the AI reads each answer while they move on, and only the closing summary may wait for it.
- Every AI-made change to the user's data (a logged session, a plan change, the profile read from onboarding) is a draft or proposal the user confirms.
- Visual design comes from Claude Design and maps onto daisyUI components.

## Primary user flows

1. **Sign in and onboarding** — Open the sign-in page → continue with Google → a new account is pending, showing only a status screen until an admin activates it → pick one sport → four short chats, each skippable: about you ("34, 168 cm, 60 kg, my left shoulder gets sore"), goals for that sport, current level and exercises, and how often and for how long to train → a climber can add the gyms they climb at and allow the location check (both optional) → "Add another sport?" offers the sports not picked yet; a second sport repeats the three sport chats but not "about you" → a summary of everything the AI read, each field editable, empty where nothing was said → confirm → land in the chat.
2. **Log a session** — Type "I climbed 3 6A and 2 6B boulders, felt strong" → the app infers the sport → a draft card shows sport, activities, grades and effort → confirm or edit → saved together with the original text. If something is ambiguous, the app offers a short list of options to pick from first.
3. **Ask a question** — "How often should I hangboard?" → an answer grounded in the knowledge base, citing its sources and taking the athlete's recent load into account. Web results, if used, are labeled.
4. **Plan** — Set a goal with a target date → a long-term plan in phases → this week's plan, fitted to the athlete's availability.
5. **Re-plan** — "I missed Tuesday" or "only 45 minutes today" → the app proposes an adjusted week → the user accepts or tweaks it.
6. **Weekly report** — A summary per sport and across sports: volume, load, highlights, goal progress and one suggestion.
7. **Log a named route** — "I sent Zygzag in Jerzmanice" → the app looks the route up in a public database, fills in the grade and the crag and shows where that came from → if the name is used at several crags, the athlete picks the right one → confirm.
8. **Plan a trip** — "I fly to Malaga, recommend me the top 10 routes between 7a and 7b+" → the app finds the crags within range and returns a ranked list: grade, ascents, onsight rate, recommendation and what climbers say about each route.

9. **Conversations** — Tap "New conversation" → an empty chat that knows nothing of the previous thread → later, open the conversation list → pick one from last week → it opens with its messages, and the next message continues it.
10. **Add my gym** — Settings → My gyms → "Murall" → bouldering and lead → boulders graded 1–9, routes in French → saved. Optionally, what each step roughly equals outdoors.
11. **Log at my gym** — At the gym, type "5 boulders all flash, and one 6 in the 3rd try" → the draft shows Murall with six boulders, one of them "6 (Murall)", not 6A → confirm. With the location check on, Murall is filled in from where the athlete is. With it off, from what they wrote or the gym they usually log at, and when that's unclear they pick the gym from a short list.

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
- **G12** — Hosted POC: one production instance serves the app over HTTPS on the owner's domain. Only the project owner can release a change to it.
- **G13** — Production data is durable: no deploy or migration loses or rewrites an athlete's saved data. The database is backed up daily, off the server, and a restore is tested.
- **G14** — Logging is quick: from sending a log to seeing its draft card takes at most 5 s at the median and 10 s at the 95th percentile, follow-up logs in the same conversation included. A log never fails because of a timeout while a model is still answering normally.
- **G15** — Europe first: v1 serves athletes in Europe. Metric units (kg, g, cm, m, km) and the European grade scales (French, Fontainebleau, UIAA, Polish/Kurtyka) are read, stored and shown reliably, and every AI capability's quality bar (G6) is measured on them. Imperial units (lb, stone, ft, in, mi) and American grade scales (V, YDS) are best effort: read where it costs little, but a gap or a miss in them never blocks a ticket, a prompt promotion or a release. A value the app cannot read is kept as typed, left empty or asked about (B3), never silently saved wrong. Units the app is unsure of default to metric.

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
- **B13** — Chat history per user, organised into conversations (B24). The router and specialists see only the current conversation's recent turns.
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
- **B24** — Conversations: every chat message belongs to one conversation. The athlete can start a new one at any time. A new conversation starts with no history in the prompt, while the athlete's profile and saved sessions still inform it.
- **B25** — Conversation history: the athlete's conversations within the retention window are listed newest first, by last activity, each with a title and its last-activity time. Any of them can be opened and continued.
- **B26** — Conversation retention: a conversation and its messages are deleted `conversation_retention_days` (default 14) after its last message. From then on, it is no longer listed or reachable. Saved training sessions, including their original text (B2), are never affected.
- **B27** — Venues: an athlete keeps their own list of climbing gyms, each with a name, the disciplines it offers (bouldering, lead, top rope) and how it grades each: its own numbers on a range, its colours, or a standard scale (French, Polish/Kurtyka, UIAA, Font, or the best-effort V and YDS, G15). A session can name the venue it happened at. Venues are the athlete's own data (G1, G7).
- **B28** — Gym grades: a grade logged at a gym that uses its own numbers or colours is stored as that gym's grade and shown as typed with the gym's name. It is never converted to Font, French or V unless the athlete has said what that gym's grades equal. A bare number with no known gym is stored as typed, with no scale. Refines B20.
- **B29** — Venue on the draft: the draft card shows the venue, inferred from the message, the athlete's only gym for that discipline when the log reads like a gym session, or the location check (B30). When several of the athlete's gyms stay plausible, a choice card offers them plus "Outdoors" and "Another gym" (B17). Otherwise it stays empty. The athlete can always change it.
- **B30** — Location check (opt-in): with the athlete's consent, a log sent while the session is happening now uses the phone's approximate position (about 100 m) to find the nearest climbing gym, and prefills the venue (B29). One gym nearby prefills it, several give a choice card, none adds nothing. With consent off, the app never reads the position.
- **B31** — The position is used for that one match and then dropped. It is never saved, logged, shown to an AI model or sent to another service.
- **B32** — Gym directory: a shared list of climbing gyms with their locations, imported in bulk from OpenStreetMap by an admin and shown with OpenStreetMap's attribution (G10). It holds no user data.
- **B33** — Athlete profile, the onboarding part: per athlete, age, height, weight and injuries or limits to plan around; per sport, goals with optional target dates (B9), current level, current exercises, sessions a week, session length and preferred days. Every field is optional. A sport's sessions, length and days are the availability that B10 plans within. The profile is the athlete's own data (G1, G7), and the features that need it read it (B9, B10, B24).
- **B34** — AI-read onboarding answers: each onboarding chat's prose is read into B33's fields by its own small AI step, in the background, while the athlete goes on to the next step. Metric answers are read reliably and imperial ones on a best-effort basis (G15); the profile is kept in metric. A detail the answer leaves unclear stays empty rather than guessed. A relative target date ("by June") resolves in the athlete's timezone (B15).
- **B35** — Onboarding summary: after the last step the athlete sees every field read from their answers, grouped as about you and one group per sport, each editable, with skipped steps marked. Nothing from onboarding is saved to the profile until the athlete confirms the summary. If a background read is still running or failed, the summary waits for it or redoes it, and shows a loading state meanwhile.
- **B36** — Voluntary onboarding data: only the first sport is required. A skipped step saves nothing and never blocks the next one. The raw onboarding answers are deleted once the summary is confirmed; only the confirmed fields stay. (B2 keeps the original text of logged sessions, not of onboarding answers.) The timezone is taken from the browser and shown on the summary to correct, instead of being a step of its own.

### Frontend (F)

- **F1** — Chat with streamed replies.
- **F2** — A draft-confirm card for logged sessions (confirm / edit / discard).
- **F3** — A history / calendar view of sessions.
- **F4** — A plan view per sport: this week and the long-term phases.
- **F5** — A report view.
- **F6** — Onboarding and profile: one sport first, then the onboarding chats (F20) and the summary (F21), the athlete's gyms (F18), the location check (F19), and account deletion (G7). The timezone comes from the browser and is corrected on the summary (B36). The display grade scale is a Settings preference (B20).
- **F20** — Onboarding chats: one card per step, styled as a smaller version of the chat, with the question, what the AI is looking for, a composer and a visible Skip. Sending an answer moves straight on to the next card. Back returns to an earlier step to change its answer. A progress indicator shows where the athlete is. The first card says that only the sport is required and the rest is optional.
- **F21** — Onboarding summary: while the answers are still being read, the summary is a glass sheet with an inner light and depth, in the style of the glass-ball loader. It then becomes an editable card with one section per group (B35), "Not mentioned" in empty fields, a "Skipped" mark on skipped sections, and one confirm button that lands in the chat.
- **F7** — Admin view: list users with their status and change a user's status (pending / active / disabled).
- **F8** — A signed-in user whose access is pending or disabled sees a neutral status screen and nothing else.
- **F9** — A choice card: 2–N concrete options, single- or multi-select, resolved by tapping, with a "let me type instead" escape.
- **F10** — A route card and a recommendation list, each showing the source and the fetch date.
- **F11** — A sign-in page with a "Continue with Google" button, and designed pages for not found (404), server error (500), signed out (401) and an expired form session (403). The unauthenticated page links to sign-in.
- **F12** — A statistics view per sport and across sports: charts and tables of volume, load and progress over a chosen date range.
- **F13** — Composer suggestions: while the athlete types, the chat offers matching suggestions to pick by tap or keyboard. What is suggested is decided per milestone; the pattern exists from the first chat release.
- **F14** — A thinking indicator: from sending a message until the reply starts streaming, the chat shows that the AI is working, naming the current step when the server reports one.
- **F15** — Sport glyph on replies: each assistant reply carries the glyph of the sport it is about, the same glyph that sport uses everywhere else (statistics, history, draft card). A reply about several sports or about none (a general question, a cross-sport report) shows the app mark.
- **F16** — A "New conversation" control in the chat, reachable in one tap on a phone.
- **F17** — A conversation list showing title and last activity, opened from the chat. It is a drawer on a phone and part of the sidebar on desktop. Picking a conversation opens it in the chat.
- **F18** — My gyms, in onboarding (climbers only, skippable) and in Settings: add, edit and remove a gym with its disciplines and grading. Grading is picked from a short list of types: numbers on a range, colours in order, or a standard scale. An optional mapping says what each step equals.
- **F19** — The location check, offered once in onboarding and switchable on and off in Settings at any time. Its explanation is short and jargon-free, along the lines of: *"Use your location to find your gym? When you log a session, we can check which climbing gym you're at and fill it in for you. Gyms grade differently, so this helps us read your grades right. We only check at the moment you send a log, never in the background, and we never save where you were. You can change this anytime in Settings."* Saying yes brings up the phone's own permission prompt. If the phone blocks location, Settings says so and explains how to allow it.

## Configuration and contracts

- All settings come from the environment / `.env` through pydantic-settings. `.env.example` is committed; `.env` never is. Indicative keys (the Settings class is the source of truth): OpenRouter API key; model ID per prompt template (router, extraction, specialists); embedding model and dimension; eval judge model; database URL; session secret; Google OAuth client ID and secret; the admin email list (`ADMIN_EMAILS`); the session merge window (`session_merge_window_min`); the conversation lifetime (`conversation_retention_days`); the public base URL of a hosted instance (`public_base_url`); the clarification option cap (`max_clarification_options`); the reference-data lifetime (`reference_data_ttl_days`); the per-user external lookup budget (`external_lookups_per_user_per_day`); the default search radius (`default_search_radius_km`); the gym match radius (`venue_match_radius_m`); how recent a session must be for the location check (`venue_location_window_min`).
- HTTP contract: an OpenAPI snapshot at `docs/api/openapi.json`, generated by a script and never hand-edited (created in M0).
- Prompt templates and eval datasets are versioned in the repository (ADR-0008, ADR-0009).

## Acceptance criteria

- [ ] A new user signs in with Google, completes onboarding and lands in the chat.
- [ ] A new user who picks a sport and skips every other step reaches the summary, confirms it, and lands in the chat with only the sport saved.
- [ ] After sending an onboarding answer, the next step's card appears without waiting for the AI to read it.
- [ ] "I'm 31, 1,78 m, 73 kg, sore left shoulder" shows age 31, height 178 cm, weight 73 kg and the shoulder under injuries on the summary.
- [ ] "Send my first 7c outdoors by June", sent in October by an athlete in `Europe/Warsaw`, shows the goal with a target date in June of the next year.
- [ ] Correcting a field on the summary and confirming saves the corrected value; nothing from onboarding is saved before the confirm, and no raw answer remains after it.
- [ ] A second sport picked from "Add another sport?" gets its own summary section and no second "about you" step.
- [ ] If the app restarts while an answer is still being read, the summary still fills in that answer's fields.
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
- [ ] A grade typed as "VII+" (UIAA) or "VI.4" (Polish/Kurtyka) is stored with its original string and its French equivalent, and displays in the athlete's chosen scale.
- [ ] A first visit renders in the dark theme; after switching to light and reloading, the page renders light with no flash of the dark theme.
- [ ] Every page renders at 390 × 844 without horizontal scrolling, and the app can be added to a phone's home screen with its icon.
- [ ] An unknown URL shows the designed not-found page with a way back to the chat.
- [ ] After sending a message, the thinking indicator is visible until the first streamed token arrives.
- [ ] "Did 4×4s on the board" produces a climbing draft without a question about the sport.
- [ ] "2 hours easy" from an athlete whose only sport is cycling produces a cycling draft.
- [ ] "Did intervals" from an athlete who does gym and cycling, with no recent context that settles it, produces a choice card offering exactly gym and cycling.
- [ ] A reply about a logged ride shows the cycling glyph; a reply to "How was my week?" across sports shows the app mark.
- [ ] After "New conversation", a follow-up that only makes sense with the earlier thread ("and 2 more of those") gets no context from it. The earlier conversation stays in the list.
- [ ] Opening a conversation from the list shows its messages, and a new message continues it and moves it to the top.
- [ ] A conversation whose last message is 15 days old is gone from the list and the database. A session saved from it is still there with its original text.
- [ ] The app is served over HTTPS on the owner's domain. A request straight to the server's IP address is refused.
- [ ] A release reaches production only after the project owner approves it. No one else can tag, approve or deploy one.
- [ ] Deploying a release that includes a migration keeps every existing athlete's sessions, goals and profile intact.
- [ ] Last night's backup restores into an empty database with the same row counts as production.
- [ ] An athlete adds "Murall" (bouldering 1–9, routes French). At Murall, "5 boulders all flash, and one 6 in the 3rd try" produces a draft at Murall with the 6 shown as "6 (Murall)", stored as a gym grade with no Font or French equivalent.
- [ ] The same message from an athlete with no gyms stores "6" as typed, with no scale and no conversion.
- [ ] After the athlete says Murall's 6 equals 6B, the same grade shows its French equivalent, and grades at other gyms are unaffected.
- [ ] "VI.3 on lead" is stored on the Polish (Kurtyka) scale.
- [ ] An athlete with two bouldering gyms who sends a gym-style log with location off gets a choice card with both gyms, "Outdoors" and "Another gym".
- [ ] With the location check on, a log sent near Murall prefills Murall; a log sent the next day about "yesterday" doesn't use the position.
- [ ] With the location check off, sending a log never triggers the phone's location prompt or reads the position.
- [ ] After a log with the location check on, no table, log line, trace, model prompt or outbound request contains the position.
- [ ] Turning the location check off in Settings takes one tap, and the next log reads no position.
- [ ] A gym from the directory shows OpenStreetMap's attribution.
- [ ] Deleting the account deletes the athlete's gyms.
- [ ] On the reference logging scenario (ADR-0025), with a five-log chain plus eight follow-ups in one conversation, log turns reach the draft card in at most 5 s at the median and 10 s at p95, and none times out.

## Success metrics

- Router intent accuracy, sport-inference accuracy and extraction field accuracy on the eval datasets; thresholds are set from the first M1 baseline.
- Share of logs that needed a sport question (lower is better, without lowering sport-inference accuracy).
- Share of logged sessions saved without edits on the draft card.
- Plan adherence: planned sessions completed per week.
- Weekly active loggers.
- Send-to-draft-card time for log turns, at p50 and p95 (G14).
- Share of gym climbing logs saved with a venue, and the share whose venue the athlete changed on the draft card (lower is better).
- Onboarding: the share of new athletes who confirm the summary, the share of steps skipped, and the share of AI-read summary fields the athlete corrected (lower is better).

## Out of scope / later

- Polish UI and replies. The seams exist: `User.locale`, a locale in prompt template file names, and a `language` field on knowledge-base chunks.
- More sports (running, swimming, …) through the plugin model.
- Wearable / Strava import.
- Other login methods, a staging environment, and high availability.
- Summarising long conversations, and LLM-written conversation titles.
- Searching conversations, or keeping one past the retention window.
- An internal human-review page for evals.
- A richer admin view: search, pagination, and a screen for the status-change history. M0 ships a minimal list with a status control.
- Offline use: a service worker, cached pages or queued messages. v1 is installable but needs a connection.
- Syncing the theme choice across devices.
- Exposing prompt templates as MCP prompts to external clients.
- Project tracking: matching a logged `attempt` on a named route to a later `redpoint`, and counting that as goal progress (B9).
- Route lookup for sports other than climbing (cycling segments, gym exercise databases). The `ExternalRouteSource` port (ADR-0016) is shaped so this is an adapter, not a redesign.
- The rest of the athlete profile, after the POC: keeping what the app knows about the athlete up to date beyond onboarding, such as the climbing disciplines they practise (boulder, lead). Until then, extraction reads that kind of context from the athlete's recent sessions (#164, B23). Owner-directed 2026-10-21. The athlete's gyms (B27) and the onboarding part (B33) are its first pieces; v0.8 brings B33 into the POC, owner-directed 2026-10-24.
- A profile page in Settings to view and edit B33's fields after onboarding, after the POC (owner-directed 2026-10-24). Until then, the onboarding summary (B35) is where the athlete corrects them.
- Highlighting summary fields the AI was unsure about, and opening the first chat with a message about the athlete's goal.
- Shared gym grading: promoting a gym's grading to everyone once several athletes agree on it. v0.7 keeps grading per athlete (ADR-0027).
- Venues for gym training (which gym's machines) and for cycling.
- Location for outdoor crags, and storing any position history.

## Delivery mapping

| Milestone | Scope | Requirements |
|---|---|---|
| M0 Foundations | Project skeleton, docker compose, CI, Google sign-in with approval gating, a minimal admin user list, LLM gateway, prompt registry, eval harness | G1, G6, G7, G8, F6 (sign-in), F7, F8 |
| M0 Design foundation | Liquid Glass theme, dark default with a remembered toggle, app shell, icons and manifest, sign-in and error pages, first end-to-end specs | G11, F11 |
| M1a Conversations | Conversations, a new-conversation control, the conversation list, continuing a conversation, retention | B13 (refined), B24–B26, F16, F17 |
| M1 Log it | Router + `log_session` path; session envelope, `SportRegistry` and the climbing, gym and cycling plugins together; sport inference; draft → confirm, and undo of a just-saved session; time awareness and session continuity; structured clarification and the choice card; composer suggestions, the thinking indicator and the sport glyph on replies; eval datasets for the router and all three extraction templates | B1–B3, B4 (create, and delete through undo), B13–B17, B23, F1, F2, F9, F13–F15, G2–G5, G9, G14, G15 |
| M1o Onboard me | The onboarding chats with background AI reading, "Add another sport?", the editable summary and its glass-sheet loading state, the onboarding part of the athlete profile; an eval dataset per onboarding AI step | B33–B36, F6 (onboarding), F20, F21, G15 |
| M1z Ship it | Production host behind the owner's Cloudflare domain, release pipeline only the owner can trigger, offsite backups, migration safety. The first production deploy waits for M1a and M1 | G12, G13 |
| M2a My gyms | The athlete's gyms with their disciplines and grading, gym grades and the Polish (Kurtyka) scale on climbing drafts, the venue on the draft card and its choice card, My gyms in onboarding and Settings | B27–B29, F18 |
| M2b Find my gym | The spike on OpenStreetMap coverage, the gym directory import, the location opt-in in onboarding and Settings, the location check on logs | B30–B32, F19 |
| M2 See it | History, with reading, editing and deleting saved sessions; cross-sport load, weekly report, statistics per sport | B4 (read, update, delete), B5, B6, B12, F3, F5, F12 |
| M3 Know it | Ingestion-pipeline ADR → ingestion → cited Q&A | B7, B8 |
| M4 Plan it | Goals, long-term and weekly plans, re-planning | B9–B11, F4 |
| M5 Explore it | Grade conversion and route identity, the 8a.nu route lookup, crag geography, area recommendations | B18–B22, F10, G10 |

G15 binds every milestone from M1 on, owner-directed 2026-10-25. The imperial-unit and V/YDS support already built stays; it is not removed, only no longer a bar to pass. Polish/Kurtyka reaches climbing drafts in M2a; UIAA, with grade conversion, in M5.

G7 is delivered in M0 and must stay complete in every later milestone: each new user-owned table joins the account deletion.

B4 is split across milestones, owner-directed 2026-10-12. M1 creates sessions and deletes a just-saved one from the chat (the Chat mock's "Undo"). Reading, editing and deleting any saved session come with M2's History view (F3), the first screen that lists them.

M2a and M2b come after M1z and before M2, owner-delegated 2026-10-22: M2's history and statistics show grades, so gym grades should be recorded right before then. M2a needs no grade conversion, so it doesn't wait for M5. B20's conversion skips gym grades (B28).

M1o comes after M1 and before M1z, owner-directed 2026-10-24: a working onboarding is a POC priority, so the first production deploy waits for it too. In onboarding, F18's gyms and F19's location check come after the climbing chats and before "Add another sport?"; until M2a and M2b they are absent.

G13 binds from the first production deploy onward. From then on, every schema change preserves existing data (ADR-0024).
