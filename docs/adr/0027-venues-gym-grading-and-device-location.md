# ADR 0027 — Venues, gym grading and device location

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-22 |
| Related | PRD-0006 (B20, B23, B27–B32, F6, F18, F19, G10), ADR-0004, ADR-0006, ADR-0014, ADR-0015, ADR-0016, ADR-0017, ADR-0018, ADR-0020, ADR-0022, ADR-0024 |

## Context

Most climbing logs come from gyms, and a gym grade means nothing without the gym. The project owner's experience of Polish gyms (owner-provided, 2026-10-22):

- **Bouldering:** every gym uses its own grades, either numbers on a closed range (1–9, 1–10) or colours. None of them uses Fontainebleau.
- **Lead and top rope:** French, and in Poland sometimes the Polish (Kurtyka, or "Krakowska") scale. thecrag gives VI.2 = 6c and VI.3 = 7a ([thecrag, Poland](https://www.thecrag.com/en/climbing/poland)).

So a bare "6" on a boulder is gym 6 at one particular gym. It is not 6A. Turning it into 6A would be wrong in a way the athlete notices. Today a bare number gets no scale and stays unconverted (`domain/sports/climbing/normalize.py`), but nothing records which gym it came from, and B20 assumes every grade has a French equivalent.

The owner also wants the app to fill in the gym from the phone's location. That is opt-in, and the position is never stored. PRD-0006 splits the work in two: venues and gym grading first (M2a), then location (M2b).

## Decision

**Venues belong to the athlete.**
- A new user-owned table, `athlete_venues`: `id`, `user_id` (FK `users` CASCADE, joins account deletion, G7), `name`, `disciplines` (a subset of `boulder`, `lead`, `top_rope`), `boulder_grading` and `route_grading` (each a `VenueGrading`, below), `directory_venue_id` (nullable FK to `venues`, M2b), `created_at`, `updated_at`. `UNIQUE (id, user_id)` and `UNIQUE (user_id, lower(name))`.
- `training_sessions` and the `session_drafts` draft gain a nullable `athlete_venue_id`, keyed with `user_id` as ADR-0022's 2026-10-13 amendment requires, `ON DELETE SET NULL (athlete_venue_id)`. The venue is part of the session envelope rather than the climbing payload, because a gym sport venue would reuse it. Adding it is an expand-only migration (ADR-0024).
- Venues are added and edited in onboarding's climbing step and in Settings → My gyms. A draft card can also create one: "new gym: Murall".

**Gym grading is its own scale, and code never converts it.**
- `VenueGrading` is one of:
  - `numbers` on a range `{min, max}`;
  - `colours`: an ordered list of colour names;
  - a standard scale: `french`, `font`, `kurtyka` or `uiaa`, or the best-effort `v` or `yds` (PRD 0008 G15).
- The climbing payload's `GradeScale` gains `gym` and `kurtyka`. A gym grade is stored as `{original: "6", scale: "gym"}`, and the session's venue says whose gym. Adding these values bumps `CLIMBING_PAYLOAD_VERSION` (ADR-0022 invariant 4).
- A `gym` grade gets `french` and `ordinal` only from a mapping the athlete has entered for that venue (each gym step → a French or Font grade). With no mapping, both stay null, in M5 too. ADR-0016's conversion module skips `gym` grades. Where a page shows grades in the athlete's display scale, it shows a gym grade as typed, with the gym's name.
- In the extraction context, the athlete's venues reach `climbing.extract` as name, disciplines and grading. The template keeps every grade exactly as typed, which it already does. Scale detection in code reads the venue: a bare number or a colour at a numbers or colours venue is `gym`. A letter grade keeps the scale it is written in. A bare number with no venue keeps `scale: null`.

**The draft's venue is a closed-set pick.**
- The draft's venue comes from one of three sources:
  - a venue named in the message;
  - the athlete's only venue that offers the climb's discipline, when the log reads indoor (a gym number or colour);
  - the device position (M2b).
- When several venues stay plausible, the athlete gets a choice card (ADR-0015): their venues, plus "Outdoors" and "Another gym". Otherwise the field stays empty.
- Picking among the athlete's venues may go to Jev (ADR-0020) where evals show it helps. The deterministic fallback is the rule above. Venue names enter the decision state, and user identity never does.

**M2b: a shared gym directory, imported in bulk.**
- A new shared reference table, `venues`, laid out like ADR-0016's tables and not user-scoped: name, city, country, latitude, longitude, `climbing:boulder` and `climbing:sport` flags, `osm_type`, `osm_id`, `source = "openstreetmap"`, `source_url`, `fetched_at`.
- A `VenueDirectorySource` port, plus an admin CLI, `uv run ai-trainer import-venues --country PL`, which pulls every climbing gym in a country through Overpass in one query and upserts the rows.
  - OSM tags climbing gyms as `sport=climbing` on `leisure=sports_centre`, `leisure=sports_hall` or `leisure=pitch`, with `indoor=yes` where mappers set it ([OSM wiki: Climbing](https://wiki.openstreetmap.org/wiki/Climbing)). The spike fixes the exact filter.
  - The import sends no user data at all, so ADR-0016 invariant 2 holds as written.
- Venues from the directory are shown with "© OpenStreetMap contributors" and a link to the ODbL ([OSM copyright](https://www.openstreetmap.org/copyright)), as G10 requires.

**M2b: device location is opt-in, used once and dropped.**
- `users.location_consent_at` (timestamptz NULL, an expand-only migration). Null means off. Onboarding and Settings set it when the athlete opts in and clear it when they opt out. Opting out is one tap, the same as opting in ([GDPR Art. 7(3)](https://gdpr-info.eu/art-7-gdpr/)), and the timestamp lets us demonstrate the consent (Art. 7(1)).
- When it is null, the page holds no geolocation code path that can run. The composer never calls `navigator.geolocation`, so the app doesn't even guess.
- When it is set, the composer calls `getCurrentPosition` once the athlete focuses it, so reading the position doesn't add to send time (G14):
  - It uses a short timeout and a few minutes' `maximumAge`.
  - It rounds latitude and longitude to 3 decimals (about 100 m) in the browser.
  - It sends them with the message as POST form fields, only when they're ready by send time. A missing position never blocks or delays a send.
- The browser's permission is separate from ours: it needs HTTPS and its own prompt ([MDN: Geolocation API](https://developer.mozilla.org/en-US/docs/Web/API/Geolocation_API)). The opt-in tap in onboarding and Settings triggers that prompt straight away. If the Permissions API reports `denied`, Settings says so and explains how to allow it.
- On the server, the position is a `GeoPoint` value object, alive only for the request. It is matched against `venues` within `venue_match_radius_m` (default 300) in Postgres. The table has a few hundred rows, so no PostGIS is needed. It is used only when the drafted session is happening now: today in the athlete's timezone (ADR-0014), and no relative-past date such as "yesterday", within `venue_location_window_min` (default 180).
  - One match: the draft is prefilled with the athlete's venue linked to that directory venue, or with "new gym: <name>".
  - Several matches: a choice card.
  - No match: nothing.

### Invariants

1. Code never converts a gym grade on its own. A `gym`-scale grade has a French equivalent or an ordinal only from the athlete's own mapping for that venue, and a bare number is never read as Font, French or V.
2. A device position is never stored, logged or sent anywhere. It never appears in a table, a chat message, a draft, an `llm_calls` row, a trace, `LLM_DEBUG_LOG`, an application log, a prompt or decision state, or a request to any external service. It arrives only in a POST body, never in a URL. A test asserts this.
3. No geolocation call runs unless `users.location_consent_at` is set. Turning location off takes effect on the next page render.
4. Position prefills a draft field and nothing more. The athlete confirms or changes the venue on the draft card, as for every AI-made change.
5. `athlete_venues` is user-scoped (ADR-0004 invariant 1). `venues` holds only public directory data and is never written from an athlete's input.
6. The gym directory is reached only through `VenueDirectorySource`, by the admin import, never per request.

## Alternatives considered

- **A shared venue table that athletes edit.** One athlete's mistake would change everyone's grades, and it would need moderation. A per-athlete table can be promoted to shared later, once several athletes agree on the same grading.
- **Querying OSM or a places API with each log's position.** That would send a user's position out of the app, against ADR-0016 invariant 2. A bulk import needs no user data. Google Places also limits how long results may be cached, which conflicts with ADR-0016's cache-first rule.
- **Converting gym grades with a generic table ("gym 6 ≈ 6B").** Every gym grades differently. That is the owner's experience above, and the reason for invariant 1.
- **Storing a coarse position on the session for later stats.** Nothing in the PRD needs it, and not storing it keeps the consent story simple.

## Consequences

- ⚠ The M2b spike runs first. It counts the climbing gyms OSM has in Poland against a known list, fixes the tag filter, and decides whether the public Overpass instance is acceptable. Its policy asks commercial users to self-host or pay ([Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API)). An import once a month is far below its limits, and a Geofabrik extract is the fallback. The spike's findings are recorded here.
- ⚠ The Kurtyka → French table needs a source the owner trusts before `grades.py` uses it. thecrag's table is the starting point.
- On acceptance, ADR-0006 and ADR-0016 note that `gym` and `kurtyka` exist and that conversion skips `gym`, and ADR-0022's Consequences note `athlete_venue_id`.
- Design: Claude Design mocks for Settings → My gyms, the onboarding climbing step (gyms and the location opt-in) and the venue chip on the draft card come before the UI tickets (`/implement-design`, ADR-0019).
- Evals: the `climbing.extract` dataset gains gym cases. "1 6 boulder in 3rd try" at a numbers gym must stay `6`/`gym`, and the same text with no venue must stay `6`/null.
- `CLAUDE.md` gains the stack line and the location non-negotiable. `.claude/rules/python.md` and `.claude/rules/llm.md` carry invariants 1 and 2.
