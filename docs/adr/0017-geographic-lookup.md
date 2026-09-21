# ADR 0017 — Geographic lookup (outline)

| | |
|---|---|
| Status | Proposed |
| Date | 2026-09-16 |
| Related | PRD-0001 (B21, B22), ADR-0010, ADR-0015, ADR-0016 |

## Context

"I fly to Malaga — recommend me the top 10 routes between 7a and 7b+" needs two things the route source alone cannot give: where Malaga is, and which climbing crags lie within reach of it (B21). Whether an existing MCP server answers that well enough, or whether a small adapter of our own is better, cannot be settled from the armchair. This ADR therefore fixes the shape and defers the provider, as ADR-0011 does for ingestion.

## Decision

- **A `GeoLookup` port** with two operations: resolve a place name to a canonical place (name, country, latitude, longitude, bounding box), and list crags within radius R of a point. `default_search_radius_km` is a setting.
- **The provider is deferred** to a spike at the start of M5, comparing:

| Option | What the spike checks |
|---|---|
| an established geo MCP server | consumed in-process with `MCPToolset` exactly as ADR-0010 does; licence, rate limits, and whether it can run offline in tests |
| our own adapter over a public geocoding / Overpass API | whether OSM tagging (`sport=climbing`, `natural=cliff`) actually locates crags, and what it costs to maintain |
| crag geography from the route source | whether ADR-0016's source already knows which crags sit near a place, making a geo provider unnecessary |

  An established server wins if one fits. The spike records the answer here, and this ADR then drops "(outline)".

- **Results are cached** in the ADR-0016 reference tables, keyed by the normalised place name, with the same provenance columns.
- **The recommendation flow is fixed** whichever provider wins: place → `GeoLookup` → crags in radius → `ExternalRouteSource` per crag → rank within the requested grade range → one typed `AreaRecommendation` with the shape B21 specifies (grade, ascent count, onsight rate, recommendation, comment summary).

### Invariants

1. Only the place name leaves the app — no user identity and no message text (ADR-0016, invariant 2).
2. Every lookup is cached with `fetched_at` and re-fetched only past the TTL.
3. An unknown or ambiguous place produces a clarification (ADR-0015), never a guess: "Malaga" the province and "Málaga" the city are different searches.
4. The provider sits behind the port. Nothing above the adapter layer names it, so the spike's outcome changes one file.

## Consequences

- ⚠ The provider is open until the M5 spike. Until then no adapter is written, and `AreaRecommendation` is developed against a fake.
- M5, after ADR-0016's source resolves a single route.
