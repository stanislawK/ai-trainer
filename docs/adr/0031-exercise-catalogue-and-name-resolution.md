# ADR 0031 — Exercise catalogue, name resolution and media links

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-30 |
| Related | PRD-0010 (B37, B38, F23, F24, G5, G10, G14), ADR-0006, ADR-0010, ADR-0015, ADR-0016, ADR-0020, ADR-0032, ADR-0033 |

## Context

The owner's first real gym session ([field transcript](../../evals/field/gym/2026-10-28-first-gym-session.md)) stored every exercise under the name as typed: "Rising straight arms on both sides", "Triceps - straightening arm bent to 90 degr". The `gym.extract` template is told to copy names exactly. The only memory of earlier sessions is `recent_exercises()` (`src/ai_trainer/domain/sports/gym/extraction.py`), which puts the last 10 sessions' names into the prompt as text.

Next week the same exercise arrives under another name, and progress (6 kg → 7 kg) can't be compared. Athletes can't be expected to know the canonical names, so the app has to know them (B37). Athletes mostly repeat the same exercises every week or two. Their own history is therefore the first and cheapest place to look, and the catalogue is needed only for what is new.

ADR-0020 already names "gym exercise name mapped onto the plugin catalogue" as a Jev pick. No catalogue exists yet.

The first catalogue is [hasaneyldrm/exercises-dataset](https://github.com/hasaneyldrm/exercises-dataset):
- 1,324 exercises, with an English name, body part, equipment, target muscle, and step-by-step instructions in ten languages, Polish among them.
- The data and texts are MIT-licensed.
- Its GIFs and thumbnails are © Gym visual. They are excluded from that license and need our own permission ([LICENSE](https://github.com/hasaneyldrm/exercises-dataset/blob/main/LICENSE), [NOTICE.md](https://github.com/hasaneyldrm/exercises-dataset/blob/main/NOTICE.md), read 2026-10-29).
- Names are English only.

## Decision

### Catalogue

Shared reference tables. The catalogue rows hold no user data (like ADR-0016's tables). An athlete's custom exercises and aliases do.

- **`exercises`**
  - `id`
  - `sport_id` (a registry ID, text)
  - `owner_user_id` (NULL for a catalogue row; set for an athlete's custom exercise; FK `users` CASCADE)
  - `source` (`exercises-dataset`, `custom`, …) and `source_ref`, unique together when `source_ref` is set
  - `name`: the canonical English name
  - `equipment`, `body_part`, `target`, `secondary_muscles`
  - `default_measure`: one of ADR-0032's measures
  - `attribution`
- **`exercise_texts`**
  - `exercise_id`, `lang`, `instructions`, `steps` (`text[]`)
  - Unique `(exercise_id, lang)`.
  - English and Polish are imported, because the source has them (G5). Other languages are not.
- **`exercise_aliases`**
  - `exercise_id`, `alias`, `alias_norm` (lower-cased, `unaccent`ed, whitespace collapsed), `lang` (nullable)
  - `user_id`: NULL for a global alias; set for one learned from that athlete (FK `users` CASCADE)
  - Unique `(user_id, alias_norm)` for user aliases. A user alias points at exactly one exercise.
- **Search indexes**
  - A GIN trigram index (`pg_trgm`) on `alias_norm` and on `lower(unaccent(name))`.
  - A `tsvector` on the name and aliases with the `simple` configuration. PostgreSQL 18 ships no Polish stemmer: the local `pg_ts_config` lists 29 languages without Polish (checked 2026-10-30).
  - `pg_trgm` 1.6 and `unaccent` are available in the project's PostgreSQL 18.6 image.
- **Seeding:** `uv run ai-trainer import-exercises`, an idempotent CLI pinned to one dataset commit in `Settings` (`7455efa` at the first import).
  - **Rows:** it upserts on `(source, source_ref)` and never touches custom rows or user aliases.
  - **`default_measure`:** derived in code from equipment (body weight → `reps`, band → `band_reps`, otherwise `load_reps`), with a committed override file, `data/catalogue/overrides.yaml`, for the exceptions (planks → `time`).
  - **Names:** the source's lower-case names are title-cased and lose a `(male)` or `(female)` suffix, which 33 of them carry (read 2026-10-31). The override file renames or skips the exceptions.
  - **Supplement:** `data/catalogue/extra.yaml` is a short, human-reviewed list of common exercises the source lacks, such as face pull, imported with `source = ai-trainer`. It grows when a real chat shows a gap and isn't filled up front.
  - **Polish names:** the dataset has none, so they come from an AI-drafted alias file (`data/catalogue/aliases.pl.yaml`). A human reviews it before it is committed. It is data, not a prompt.

### Resolution order (B37)

`ResolveExercises`, an application use case, runs in code after extraction, on the logging hot path. It costs at most one Jev call per unresolved exercise, so G14 holds. For each extracted activity it tries these steps in order:

1. **History, linked.** The typed name's `alias_norm` matches one of the athlete's aliases, or an earlier entry of theirs that already carries an `exercise_id` (ADR-0032). Reuse that exercise with no question.
   - This searches the athlete's whole alias set and linked history, not only the last 10 sessions.
   - A near match (trigram similarity above a `Settings` threshold) against the athlete's own aliases counts. It is shown on the draft card as "you usually log this as …".
2. **History, without a universal name.** The match is an earlier entry stored by name only: a legacy payload or a typed-only row.
   - Identify it with the athlete once, through step 3's shortlist and card.
   - On confirm, store the alias and link every earlier typed-only entry of theirs with the same `alias_norm`.
3. **Not in history.** Build a shortlist of at most `exercise_shortlist_size` (default 4) candidates from the catalogue and the athlete's custom exercises:
   - by trigram and `simple` full-text search over names and aliases in every language;
   - narrowed by the extracted equipment and load when present.

   Only candidates above a `Settings` floor count, and with none Jev isn't called.

   Jev then picks one candidate or "none of these". This is a closed-set draft field (ADR-0020), added with `/add-decision`.
   - **Confident pick:** the draft card shows the canonical name with "you wrote: …" under it. Confirming the draft confirms the mapping and stores a user alias.
   - **Below threshold, "none", a timeout or an error:** an F23 choice card (ADR-0015). It offers the candidates, each with a short description taken from `exercise_texts` and media when approved media exists, plus "Keep my name" and "let me type instead". With no candidate it offers only those two.
   - **"Keep my name":** creates a custom exercise owned by the athlete.

The draft carries `exercise_id` per activity. The typed name always stays as `name_as_typed`. The MCP tools `find_exercise` and `exercise_history` (ADR-0010) wrap the same use case for specialists and external clients.

**The shortlist is best effort until the knowledge base lands.** Owner-directed 2026-10-31. Guessing a name the athlete paraphrased is not a bar for M1g:
- A missed guess costs one tap on "Keep my name", and history remembers the answer from then on.
- M1g sets no recall target and evaluates the shortlist no further than the decision set's smoke set of 5 cases (ADR-0034).
- Better matching comes from M3's search over the catalogue texts (ADR-0033), and is measured then, on real data.

**No language step.** Matching runs over aliases and texts in every language, and trigram search needs no language. A Jev language check would add a round trip to the hot path and decide nothing that matching needs. If a later feature needs the language, a local detector library is cheaper than a model call.

### Media links (F24)

- **`media_assets`**
  - `id`, `storage_key`, `kind` (`gif`, `image`, `drawing`), `source`, `source_ref`, `license`, `attribution`, `sha256`, `width`, `height`
  - Unique `sha256`.
- **`exercise_media`**
  - `exercise_id`, `asset_id`, `position`, `status` (`suggested`, `approved`)
  - Unique `(exercise_id, asset_id)`.

Media is linked to the catalogue exercise, never to a name. Every alias, history entry and custom exercise mapped to that exercise gets it for free.

- **Same source as the catalogue row:** links by `source_ref` and is `approved` on import.
- **Other sources:** get candidate exercises from trigram similarity, and from embeddings once ADR-0033's model is chosen. Jev confirms or abstains, and links are written as `suggested`. An admin approves them in bulk at `/admin/exercises`.
- **Storage:** the media bucket behind `ObjectStoragePort` (ADR-0033). It holds no user data, so it is served publicly through an R2 custom domain with Cloudflare caching, never `r2.dev`, which Cloudflare reserves for non-production traffic ([R2 public buckets](https://developers.cloudflare.com/r2/buckets/public-buckets/)). The attribution is shown next to the media.

No media is imported until a source with a license we hold is chosen. That is a ticket, not this ADR.

### Invariants

1. A mapping from a typed name to an exercise is saved only when the athlete confirms it: on the draft card, or by picking on the choice card. Jev's pick alone never writes an alias.
2. History is checked before the catalogue. A name the athlete already confirmed is never asked about again.
3. User aliases and custom exercises are the athlete's own data: user-scoped, deleted with the account (G1, G7). Catalogue queries read rows where `owner_user_id IS NULL OR owner_user_id = :user_id`.
4. The typed name is always kept next to the resolved exercise.
5. Imported text and media keep their source, license and attribution, and only `approved` media is shown (G10, ADR-0016).
6. No exercise name, alias or description is written by a model at request time. Descriptions come from `exercise_texts`.

## Alternatives considered

- **Let the extraction LLM name the exercise from a catalogue in its prompt.** 1,324 names don't fit a lean prompt (G14), and an LLM would write names freely rather than pick.
- **An external exercise API at request time (ADR-0016's port).** That adds a network call to every log. The data changes rarely, so an import is enough.
- **Embedding search for names from day one.** It needs ADR-0033's model first. Trigram covers typos and word order for short names, and embeddings join the shortlist later without a schema change.
- **A search phrase returned by `gym.extract`, used only for matching.** It might rescue paraphrases at no extra call, but it adds a prompt field to evaluate for a problem M3 addresses. Left out, owner-directed 2026-10-31.

## Consequences

- `gym.extract` keeps copying the name as typed, which history matches on (invariant 4), and gains the measures (ADR-0032). Resolution names the exercise. The new template version goes through `/tune-prompt` (ADR-0034).
- ⚠ Expect paraphrases to miss, accepted above. A Python approximation of `pg_trgm` on the pinned dataset, 2026-10-31, ranks `dumbbell lateral raise` 178th among dumbbell exercises for "Rising straight arms on both sides", and "Face pull" isn't in the dataset. Plain names such as "Bench press" and "Plank" do find the right family of exercises.
- The migration adds `CREATE EXTENSION IF NOT EXISTS pg_trgm` and `unaccent`. Both are expand-only (ADR-0024).
- ADR-0020's catalogue candidate is delivered here. The set starts at a conservative threshold and is tuned from feedback (ADR-0034).
- The F23 card needs a Claude Design mock with a media slot before its web ticket (ADR-0019).
- ⚠ The media source (a Gym visual license, an openly licensed set, or drawings) is a spike ticket. Until it lands, cards show text only.
- When M3's search improves the shortlist, whether to offer a catalogue match for an athlete's existing custom exercises is decided then. The custom row and its entries stay valid either way.
- A `cycling` or `climbing` catalogue can reuse these tables through `sport_id` when a PRD asks for one.
- Verified at the import ticket (#375), on the pinned dataset, 2026-10-31:
  - **Index function.** `unaccent` is only `STABLE`, so an index can't call it. The migration adds `exercise_name_norm(text)`, an `IMMUTABLE` wrapper with an explicit dictionary that also collapses whitespace. The name index uses it, and `alias_norm` is a column generated from it, so a later lookup that calls the same function can't normalize differently from a stored alias.
  - **Source defects, handled in `overrides.yaml`.** Eight pairs of rows share a name after cleaning, so one of each is skipped: 1,324 − 8 + 1 (Face Pull) = 1,317 rows. Four names hold a mis-encoded degree sign (`45в°`) and one a stray `_shoulder`; they are renamed. The import refuses two rows with one name and an override naming an unknown row, so a pin bump can't add either unnoticed.
  - **Attribution.** Every source row's `attribution` is the media credit `© Gym visual`, which doesn't apply to the data. Rows store our own text naming the MIT-licensed data, the commit and the fetch date (G10), and `data/catalogue/LICENSE.exercises-dataset` keeps the notice MIT requires.
  - **Measures.** `resistance band` counts as a band, like `band`. Other equipment follows the `load_reps` rule, so a stability-ball crunch defaults to `load_reps` with its load left empty.
