# ADR 0016 — External reference data: crags, routes and grades

| | |
|---|---|
| Status | Proposed |
| Date | 2026-09-16 |
| Related | PRD-0001 (G10, B18–B22), ADR-0004, ADR-0006, ADR-0007, ADR-0011, ADR-0015 |

## Context

"I sent Zygzag in Jerzmanice" carries a grade the athlete never typed: the route is 7c, and a public database knows it (B18). The same data answers "how hard was that?" and feeds area recommendations (B21). It describes the world, not the athlete — closer to the knowledge base than to a session — and fetching it costs money and leaves the machine, so it needs a cache, an attribution trail and a privacy rule (G10, B19, B22).

## Decision

- **An `ExternalRouteSource` port** in the application layer. The first adapter resolves routes and crags through the web search capability of ADR-0007, constrained to the 8a.nu domain, and returns a typed Pydantic model.
- **Shared reference tables** — `crags`, `routes`, `places` — which are not user-owned:

| Column group | Contents |
|---|---|
| identity | name, `aliases` (spelling and diacritic variants), crag, sector, country |
| location | latitude, longitude (ADR-0017) |
| grade | original string, scale, canonical French, ordinal (ADR-0006) |
| community | ascent count, onsight rate, rating, a short summary of comments |
| provenance | `source`, `source_url`, `fetched_at`, content hash |

- **Cache first.** A lookup reads the tables; only a miss, or an entry older than `reference_data_ttl_days`, reaches the network. `external_lookups_per_user_per_day` in settings caps the spend.
- **Grade conversion** lives in one module, `src/ai_trainer/domain/grades.py`: French, Fontainebleau, V-scale, YDS and UIAA, each mapped to the shared ordinal. "Between 7a and 7b+" becomes an ordinal range. French is canonical (B20) and the display scale is a profile preference.
- **The athlete's own grade wins.** A fetched grade fills a blank, or is offered as a correction on the draft card; it never overwrites what the athlete typed. Community grades are consensus, not truth.
- **Two prompt templates** (ADR-0008): `climbing.route_lookup` and `climbing.area_recommend`, each with an eval dataset before it goes active (G6).

### Invariants

1. Every stored fact carries `source`, `source_url` and `fetched_at`, and is shown with that attribution (G10, B8).
2. Only a route, crag or place name leaves the app — never a user identity, a message, or profile data.
3. Reference tables are never user-scoped. ADR-0004 invariant 1 does not apply to them, because they hold no user data and nothing that account deletion must reach (G7).
4. A miss, a low-confidence match or several matches produces a clarification (ADR-0015), never an invented grade or crag.
5. Tests never reach the network: the port is faked in unit tests, and parsing is covered by recorded fixtures (ADR-0013).

## Consequences

- ⚠ How to query 8a.nu efficiently — which page answers a lookup in a single search, whether a crag page beats a search page, and what its terms permit — is the first M5 ticket, a spike. Its findings are recorded here before the adapter is written. If the terms do not allow this, the port stays and the adapter changes.
- ADR-0006 gains route identity and the French-canonical grade rule; ADR-0007's web search widens beyond Q&A.
- `.claude/rules/llm.md` carries the privacy and attribution rules.
