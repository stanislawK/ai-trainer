# ADR 0031 — Exercise catalogue, name resolution and media links

| | |
|---|---|
| Status | Accepted |
| Date | 2026-10-30 |
| Related | PRD-0010 (B37, B38, F23, F24, G5, G10, G14), PRD-0012 (F24, G16, G17), ADR-0006, ADR-0010, ADR-0015, ADR-0016, ADR-0020, ADR-0032, ADR-0033 |

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
- Better matching comes from M3's search over the catalogue texts (ADR-0033), and is measured then, on real data. It was measured at #429 (see the amendment under Consequences): the search changes which candidates a paraphrase gets, not whether the right one is among them, so the shortlist stays best effort. At #508 it found one of the two field paraphrases, not the other, and it stays best effort.

**No language step.** Matching runs over aliases and texts in every language, and trigram search needs no language. A Jev language check would add a round trip to the hot path and decide nothing that matching needs. If a later feature needs the language, a local detector library is cheaper than a model call.

### Media links (F24)

- **`media_assets`**
  - `id`, `kind` (`gif`, `image`, `drawing`, `video`), `rights` (`licensed`, `unstated`), `delivery` (`stored`, `embedded`), `storage_key`, `embed_url`, `source`, `source_ref`, `license`, `attribution`, `sha256`, `width`, `height`
  - CHECK: `stored` has `storage_key` and `sha256` and no `embed_url`; `embedded` has `embed_url` and no `storage_key`; `licensed` has `license` and `attribution`; `unstated` is always `embedded`.
  - Unique `sha256` where set, unique `embed_url` where set.
- **`exercise_media`**
  - `exercise_id`, `asset_id`, `position`, `status` (`suggested`, `approved`, `removed`)
  - Unique `(exercise_id, asset_id)`.

Media is linked to the catalogue exercise, never to a name. Every alias, history entry and custom exercise mapped to that exercise gets it for free.

- **Same source as the catalogue row:** links by `source_ref` and is `approved` on import.
- **Other sources:** get candidate exercises from trigram similarity, and from embeddings once ADR-0033's model is chosen. Jev confirms or abstains, and links are written as `suggested`. An admin approves them in bulk at `/admin/exercises`.
- **Storage:** the media bucket behind `ObjectStoragePort` (ADR-0033). It holds no user data, so it is served publicly through an R2 custom domain with Cloudflare caching, never `r2.dev`, which Cloudflare reserves for non-production traffic ([R2 public buckets](https://developers.cloudflare.com/r2/buckets/public-buckets/)). The attribution is shown next to the media.

No media is imported from a source PRD 0012 G16 puts *out*, without permission. The first source chosen under G16 is below; later sources follow the bullets here.

- **`licensed`** (our own drawings, an open licence, or permission we hold) may be `stored` in the media bucket, or `embedded`.
- **`unstated`** (published freely by its owner, no licence stated) is only `embedded`: the card uses the source's own embed, such as YouTube's player, or a link to its page. Nothing is downloaded or re-hosted, and the source's own measures against framing or hotlinking are respected; where they apply, the card links out instead. The card shows "rights not confirmed" in place of a licence.
- Media G16 puts *out* is not imported, as either kind, without permission.
- **`removed`** is the takedown path. It hides the link at once. For `stored` media it also moves the object out of the public bucket to a private quarantine key and purges its Cloudflare cache entry, so the public URL stops serving it. The row is kept, so a removal can be audited and undone, and an importer skips a `sha256` or `embed_url` that is `removed`.

#### First media source (#514)

Owner-directed 2026-11-07, at #514. Evidence: [exercise-media.md](../research/exercise-media.md).

- **Source:** `wger` images ([API](https://wger.de/api/v2/exerciseimage/)). **Tier:** `licensed` (G16). **Delivery:** `stored` in the media bucket. `license` is each image's own: CC BY-SA 4 or CC BY-SA 3 (every image read on 2026-11-02 was one of the two).
- **Attribution**, shown beside the image: `<author> · <licence name> (<licence URL>) — via wger.de`, where "wger.de" links to the image's own page (its `source_ref` URL). Author, licence name and URL come from that image's own record into `attribution` and `license`.
- **Use rule:** the file is stored and served byte for byte (no re-encoding or resizing at import, so `sha256` is of wger's file) and scaled by CSS only, with the credit and licence link beside it. Whether that avoids ShareAlike is not settled by any source read. The owner accepts the remaining risk as a non-commercial side project (G17), and a rights holder's removal request is honoured at once through `removed` (G16).
- **Skipped at import:** an image with no author, because a `licensed` row needs an `attribution` the CHECK accepts and the credit has no author to name. Owner-decided 2026-11-07.
- **AI-flagged images stay eligible:** an image wger flags as AI-generated is imported with the flag kept in `source_ref`, and the card adds "AI generated" beside the credit. The admin judges it in F24 review like any other picture. Owner-decided 2026-11-07.
- **Rejected:** Gym visual, because the owner will not pay for content in a side project, so G16 *Out* holds until permission is held. `free-exercise-db`'s images, because G16 puts them *Out*: its maintainer does not know their origin ([issue #2](https://github.com/yuhonas/free-exercise-db/issues/2)) and finding 1 of the research calls them likely infringing.
- **The import (#552).** Owner-decided 2026-11-11, at #552's plan; matching amended at #555. `ai-trainer import-wger-pictures [--limit N] [--admin <email>]` reads wger's images, licences and English exercise names, and links each picture to a shared catalogue exercise:
  - **Shortlist** (#555): catalogue-only plain trigram similarity of wger's English name against each catalogue exercise's name and catalogue aliases (never an athlete's), at `EXERCISE_CANDIDATE_FLOOR` (0.25), at most `EXERCISE_SHORTLIST_SIZE` (4), best first. The athletes' shortlist's full-text term is left out, because it would tie every name holding the typed words, and so is the search by meaning, an embedding call per name for a best-effort list.
  - **Jev picks** (#555): `exercise_shortlist` v1, unchanged, asked with the name alone as its state. Only a pick at or above `EXERCISE_PICK_THRESHOLD` (0.9) is linked, as `suggested`; the admin's F24 review still approves it. No English name, no candidate, "none", a pick below the threshold, a timeout or an error leaves the picture `unmatched`, and nothing is downloaded or stored. Each name is asked once per run.
  - **Admin and accounting** (#555), as *Picture generation*: `--admin` defaults to the one `ADMIN_EMAILS` entry and must be listed there and belong to an active account, checked before any request or call (`refused reason=admin_required|not_admin`). Each Jev call writes one `llm_calls` row under that admin. The command is outside ADR-0029's per-user budget, and `--limit` bounds its calls.
  - **`source_ref`** is the image's API record URL, `https://wger.de/api/v2/exerciseimage/<id>/`, with `#ai-generated` appended when wger flags the image.
  - **`license`** is the licence's `short_name` from `/api/v2/license/`. An image whose licence id is not in that list is skipped (`unknown_licence`), like one with no author.
  - A `sha256` already in `media_assets`, a `removed` one included, is skipped (`known_sha256`), so a second run writes nothing.

#### Fallback media source (#524)

Owner-directed 2026-11-08, at #524. Evidence: [exercise-media.md](../research/exercise-media.md), finding 8.

- **Model:** `google/gemini-3.1-flash-lite-image` on OpenRouter, $0.0336 per picture (tool-printed, 2026-11-08). **Tier:** `licensed` (G16): our own generated output, with no third-party licence. `license` names Google's terms: "Google won't claim ownership over that content" ([Gemini API additional terms](https://aistudio.google.com/docs/terms), read 2026-11-08). **Delivery:** `stored` in the media bucket. **`kind`:** `image`, because the model returns a JPEG (1024×1024), not a drawing or a GIF; the enum above is unchanged.
- **Attribution**, shown beside the picture: `AI generated with Gemini 3.1 Flash Lite Image (Google) via OpenRouter · <date>`, where `<date>` is the generation date. Google's terms say use of generated content "may require" attribution to users, so the credit is always shown.
- **Approval (F24):** each picture is written `suggested`, never `approved` on import, and an admin approves it one picture at a time before it shows. It is never bulk-approved. Gemini printed wrong labels on some samples (finding 8), so the admin checks each picture for that.
- **Stable/GA:** `get-model`, 2026-11-08, shows no preview, beta or `-exp` marker in the ID and no `expiration_date`. The dated slug `google/gemini-3.1-flash-lite-image-20260630` is pinned in `source_ref`.
- **Risk:** whether such pictures can be copyrighted at all is not settled (finding 4). The owner accepts that as a non-commercial side project (G17), and `removed` is the takedown path (G16).
- **Scope:** `wger` stays the first source. This one only fills gaps where `wger` has no picture for an exercise.

#### Picture generation (#531)

Owner-directed 2026-11-08, at #531. Evidence: [exercise-media.md](../research/exercise-media.md), finding 8. ⚠ The names below (template id, setting, one cap per run) are proposals for the owner's review.

- **Flow:** two steps. A text model fills the template `media.picture_brief` with the exercise's description and writes a brief. Then the image model of #524 draws from that brief.
- **Brief input:** an exercise name plus public text only. Never athlete data.
- **Description sources:** `exercise_texts` and `search_knowledge` passages only. An internet lookup is out for now (owner, 2026-11-08).
- **Brief rules:** the start and end position, one figure, a plain background, no printed text or labels. Gemini printed wrong labels from a bare-name prompt (finding 8).
- **Port:** `ImageGenerationPort`, a `typing.Protocol` in `application/ports/`. Its adapter is in `adapters/`, over OpenRouter's images endpoint (`/api/v1/images`), wired in the composition root (ADR-0003). The text step uses the existing gateway.
- **Accounting:** an `llm_calls` row per call (ADR-0018), recording the admin who runs the generation, as admin-run ingestion does. The command takes the admin's email and checks it against `ADMIN_EMAILS`; it refuses otherwise, so no unverified `user_id` is written. Traces are content-free. The image adapter has its own timeout, error translation and accounting, since it does not go through the text gateway.
- **Cap:** `Settings.image_generation_spend_cap_usd`, one cap per run, summed from the run's own `llm_calls` cost. A call whose cost is unknown (null) stops the run, so the cap can't be passed by missing figures. The run stops before any call that would take the spend over the cap, using the model's price from `Settings`.
- **No description:** an exercise with neither an `exercise_texts` row nor a `search_knowledge` passage is skipped and listed in the run's report, never drawn from its name alone (finding 8).
- **Run on purpose:** an admin-run command, never from a chat, a page or any request. Nothing is generated at request time, and every picture is written `suggested` for admin approval (#524). Invariant 6 is not the basis for this: it covers names, aliases and descriptions, not pictures.
- **Cap hit:** the run stops before the next call and keeps the pictures already made as `suggested` (approval rule above). It reports the number done and the spend. Raising the cap is the owner's edit of `Settings`.
- **Budget (owner-directed 2026-11-08, at #533):** the command is outside ADR-0029's per-user model-call budget, as admin-run ingestion's embeddings are. Its spend cap and one exercise per run bound it instead. The picture's price in `Settings` (`IMAGE_GENERATION_PRICE_USD`, 0.0336 USD) only checks the cap before the call; the row records the cost the response bills (`usage.cost`).

### Invariants

1. A mapping from a typed name to an exercise is saved only when the athlete confirms it: on the draft card, or by picking on the choice card. Jev's pick alone never writes an alias.
2. History is checked before the catalogue. A name the athlete already confirmed is never asked about again.
3. User aliases and custom exercises are the athlete's own data: user-scoped, deleted with the account (G1, G7). Catalogue queries read rows where `owner_user_id IS NULL OR owner_user_id = :user_id`.
4. The typed name is always kept next to the resolved exercise.
5. Imported text and media keep their source, license and attribution, and only `approved` media is shown (G10, ADR-0016). `unstated` media is only ever `embedded`, and is shown only while the app is non-commercial (PRD 0012 G16, G17); hiding it is one query on `rights`.
6. No exercise name, alias or description is written by a model at request time. Descriptions come from `exercise_texts`.

## Alternatives considered

- **Let the extraction LLM name the exercise from a catalogue in its prompt.** 1,324 names don't fit a lean prompt (G14), and an LLM would write names freely rather than pick.
- **An external exercise API at request time (ADR-0016's port).** That adds a network call to every log. The data changes rarely, so an import is enough.
- **Embedding search for names from day one.** It needs ADR-0033's model first. Trigram covers typos and word order for short names, and embeddings join the shortlist later without a schema change.
- **A search phrase returned by `gym.extract`, used only for matching.** It might rescue paraphrases at no extra call, but it adds a prompt field to evaluate for a problem M3 addresses. Left out, owner-directed 2026-10-31.

## Consequences

- `gym.extract` keeps copying the name as typed, which history matches on (invariant 4), and gains the measures (ADR-0032). Resolution names the exercise. The new template version goes through `/tune-prompt` (ADR-0034).
- ⚠ Expect paraphrases to miss, accepted above. A Python approximation of `pg_trgm` on the pinned dataset, 2026-10-31, ranks `dumbbell lateral raise` 178th among dumbbell exercises for "Rising straight arms on both sides", and "Face pull" isn't in the dataset. Plain names such as "Bench press" and "Plank" do find the right family of exercises. At #429 the catalogue search ranks it 5th, still outside the four candidates (2026-11-07, below). At #508 it is the shortlist's 3rd; the triceps paraphrase still misses (2026-11-07, below).
- Reading sets and reps (owner-directed 2026-10-31, #373): "3x8", "3 x 8" and "3×8" are 3 sets of 8 reps, the first number being the sets. After a number of reps, an "x" attached to the next number counts the sets: "8 x3" and "8 pull-ups 3x" are 3 sets of 8. A lone "x5" after a load or a name ("140kg x5") is 5 reps. The owner narrowed the plan-gate wording after review, because "an x attached to one number counts the sets" also read "140kg x5" as 5 sets. The athlete corrects any misread on the draft card.
- The migration adds `CREATE EXTENSION IF NOT EXISTS pg_trgm` and `unaccent`. Both are expand-only (ADR-0024).
- ADR-0020's catalogue candidate is delivered here. The set starts at a conservative threshold and is tuned from feedback (ADR-0034).
- The F23 card needs a Claude Design mock with a media slot before its web ticket (ADR-0019).
- ⚠ The media source is `wger` (see "First media source (#514)", 2026-11-07). The import is the M2m ticket's work. Until it lands, cards show text only.
- ⚠ A fallback source fills `wger`'s gaps (see "Fallback media source (#524)", 2026-11-08). The generation flow, the brief rules against printed text, and the spend cap are recorded in "Picture generation (#531)", 2026-11-08. The import and the code are M2m's work.
- Owner-directed amendment, 2026-11-11, at #555, approved by the owner at its plan gate: in "The import (#552)" the **Matching** and **Jev is deferred** bullets are replaced by a catalogue-only shortlist, Jev's pick at `EXERCISE_PICK_THRESHOLD` with unmatched as its fallback, and `--admin` with `llm_calls` rows under the admin, outside ADR-0029's budget and bounded by `--limit`. It changes no other Decision or Invariant.
- Owner-directed amendment, 2026-11-11, at #552: it adds "The import (#552)" under "First media source (#514)" and changes no other Decision or Invariant.
- Owner-directed amendment, 2026-11-08, at #531: it adds "Picture generation (#531)" and changes no Decision or Invariant.
- Owner-directed amendment, 2026-11-08, at #533: in "Picture generation (#531)" the open budget question is settled. The command is outside ADR-0029's per-user budget, bounded by its spend cap and one exercise per run. It changes no other Decision or Invariant.
- Owner-directed amendment, 2026-11-07, with [PRD 0012](../prd/0012-ai-training-companion.md) (F24, G16, G17). It is proposed for the owner's review with this branch's PR and holds once PRD 0012 is Approved. The app is a non-commercial side project, so media its owner published freely without stating a licence may be embedded after an admin approves it. This adds `rights`, `delivery`, `embed_url` and `removed` above, relaxes "a license we hold" and invariant 5, and leaves the admin gate and the attribution rule for licensed media as they were.
  - **Why embed, not copy.** Under EU case law, linking to or framing a work its owner made freely available is not a communication to the public, unless it gets around a measure against framing. The cases are *Svensson*, *BestWater* and *VG Bild-Kunst*. A non-profit linker's lack of knowledge that a source infringes counts in its favour, but is not presumed, and a linker who knew or ought to have known is liable (*GS Media*). See [exercise-media.md](../research/exercise-media.md), finding 7.
  - **Sources under this rule.** `wger`'s CC BY-SA images are `licensed`. Gym visual's GIFs stay out: the dataset's [NOTICE.md](https://github.com/hasaneyldrm/exercises-dataset/blob/main/NOTICE.md) excludes them from its MIT licence (read 2026-10-29). `free-exercise-db`'s images stay out as well, even as links, since its maintainer does not know their origin ([issue #2](https://github.com/yuhonas/free-exercise-db/issues/2), read 2026-11-02) and finding 1 calls them likely infringing. Either can become `licensed` with written permission.
  - ⚠ This is a policy choice, not legal advice. Photos of real people also touch their right to their own image (art. 81 of the Polish copyright act), which a link does not settle; a request is honoured through `removed`.
- When M3's search improves the shortlist, whether to offer a catalogue match for an athlete's existing custom exercises is decided then. The custom row and its entries stay valid either way.
- A `cycling` or `climbing` catalogue can reuse these tables through `sport_id` when a PRD asks for one.
- Owner-approved 2026-10-31, at #397's plan gate. Resolution without Jev (#399 adds it):
  - **Everything is written at confirm.** A tap changes only the draft. The confirm's transaction, in `GymActivityStore.add`, stores the alias, links earlier typed-only entries of that `alias_norm`, and creates the custom exercise for a kept name. This is stricter than invariant 1, which also allows saving at the pick, so a discard leaves nothing behind. The draft carries `keep_name` for it, which is gym payload v3: additive, so a v2 document reads unchanged with no upcast.
  - **Step 1 reads aliases only.** Every linked entry gets its alias in the transaction that links it, so "an earlier entry that carries an `exercise_id`" needs no lookup of its own, and the resolver never reads the gym's tables (ADR-0032).
  - **Thresholds,** measured with `pg_trgm` on the imported catalogue:
    - `exercise_near_match_threshold` is 0.75. `bench pres` (0.77) and `hammer curls` (0.79) pass. `incline` against `decline bench press` (0.67) and `bench press` against `incline bench press` (0.60) don't. `pull ups` (0.70) and `przysiady` (0.73) get one card, the safe side.
    - `exercise_candidate_floor` is 0.25. `RDL` tops out at 0.14 and nonsense at 0.06, so both get no candidates. A name holding every word of the typed one counts as at the floor, by `simple` full text.
  - **"Narrowed by equipment and load"** means candidates of the draft's own measure sort first, with no hard filter. `gym.extract` extracts no equipment, so the measure is the only signal. By similarity alone, "bench press 60kg" lists Band Bench Press first (0.75).
  - **Undo keeps the alias, the links and any custom exercise.** The mapping is the athlete's confirmed answer, not part of the log, the same rule as a band (ADR-0032).
  - **A name past 200 characters stays typed-only,** with no question: the alias and exercise columns can't hold it.
  - **Near-match and "you wrote" text wait for #402** (F23). Until then a near match fills `exercise_id` with nothing on the card to say so. #402 can tell one apart: a filled `exercise_id` with no exact alias.
  - The latency scenario's athlete is climbing-only, so it never reached this code. `make latency-scenario sport=gym` replays gym logs for this path's G14 evidence, recorded below (#415).
- Owner-approved 2026-11-01, at #402's plan gate (F23). This delivers #397's "Near-match and 'you wrote' text wait for #402":
  - **The card reads names when it renders.** The draft's canonical name and alias match come from a user-scoped `labels` lookup per draft, so nothing new goes in the gym payload. That is two queries per shown gym draft, which a conversation holds few of, and a discarded or undone draft reads none.
  - **Telling a near match apart.** A filled `exercise_id` with no exact alias isn't enough, because a candidate tapped on this draft looks the same until the confirm writes its alias. A near match therefore also needs an alias of that exercise at `exercise_near_match_threshold` or above, on a name the athlete didn't answer about on this draft. It shows that alias: "you usually log this as side raises".
  - **The other lines.** Every other settled name shows "you wrote: …", unless it reads the same as the canonical name. A kept name shows neither. The logged card keeps the canonical name and drops these lines, as the Gym Measures mock does.
  - **How-to text.** A candidate's how-to is the start of its English `exercise_texts.instructions` (G5), clamped to two lines by the card and stored on the option's `params`. A custom exercise has no text, so its row has no how-to line.
  - **Media.** `media_assets` and `exercise_media` don't exist until M2m, so the card has no media markup at all, and therefore no empty slot.
  - **"Use this name"** keeps the typed name as "Keep my name" does (ADR-0015, the #402 amendment).
- **Gym G14 evidence (#415, 2026-11-02).** ⚠ G14 isn't fully met on gym logs. Its other sentence fails: one log in 40 failed on the router's 5 s timeout, which #436 carries. Its time bounds hold as measured here, 1.9 s at the median against 5 s and 4.5 s at p95 against 10 s, but only up to the first card the athlete sees. G14 says "draft card", and on the 16 turns with a new name that first card was the exercise card, so the draft card came after the athlete's tap, outside these figures. Tapping calls no model.
  - **Setup.** `make latency-scenario sport=gym`, four runs of 10 log turns, on `main` at `de9faa5` plus this ticket's script fix. `.env.example`'s models, routing and timeouts: `z-ai/glm-5.3-flash` on `{"sort":"latency","quantizations":["fp8","nvfp4"]}`, 5 s for the router and 30 s for extraction, and Jev at 2 s. The worktree's own database held the imported catalogue of 1,315 rows. Between runs only the scenario's athlete was deleted, leaving no aliases and no custom exercises, so each run met new names. Percentiles are the scenario's nearest rank, failed turns included.

    | Run | p50 | p95 | Timeouts | Failures | Turns with `exercise_shortlist` |
    |---|---|---|---|---|---|
    | 1 | 2.1 s | 3.5 s | 0 | 0 | 1, 2, 5, 9 |
    | 2 | 1.8 s | 6.8 s | 1 (turn 6) | 0 | 1, 2, 5, 9 |
    | 3 | 1.9 s | 3.2 s | 0 | 0 | 1, 2, 5, 9 |
    | 4 | 1.8 s | 4.5 s | 0 | 0 | 1, 2, 5, 9 |
    | **Pooled, 40 log turns** | **1.9 s** | **4.5 s** (max 6.8 s) | **1** | **0** | |

  - **USD per log turn,** min / median / mean / max: 0.000000 / 0.000148 / 0.000174 / 0.000311. The minimum is the timed-out turn, whose router row records no cost.
  - **Turns, p50 / p95:**
    - with a Jev pick on a new name (16): 1.9 s / 4.5 s;
    - answered from history (24, counting the timed-out turn, which never got as far as naming): 1.7 s / 5.4 s;
    - through the router (12): 3.1 s / 6.8 s;
    - without it (28): 1.8 s / 2.2 s.
  - **Calls, p50 / p95 / max:**
    - `exercise_shortlist` (20): 242 / 322 / 423 ms;
    - `gym.extract` (39): 1277 / 1793 / 1853 ms;
    - `router` (12): 1056 / 5005 / 5005 ms, with one timeout;
    - `router_fast` (40): 256 / 397 / 1583 ms;
    - `safety_screen` (40): 246 / 331 / 421 ms.
  - **Naming isn't the slow step; the router is.** Resolution adds about a quarter of a second per new name, and history answered every repeat with no Jev call (invariant 2). All 12 router calls came from the three logs per run that name two exercises in one message. Run 2's turn 10 took the router 4.7 s for 384 output tokens, against 58–196 on the other successful calls. Whether the timed-out call was a slow upstream or long reasoning can't be told from this run: it logged no tokens and no upstream.
  - **What these figures leave out:**
    - Jev's pick wasn't confident on any name: run 4 ended with 5 answered questions for its 5 names, at `exercise_pick_threshold` 0.9, so each new name costs a tap. Only run 4 was checked, because deleting its athlete removes a run's questions. The threshold is tuned from feedback (ADR-0034), not here.
    - The athlete is gym-only, so a multi-sport athlete's first log also goes through the router (ADR-0026).
  - **Running it.** Import the catalogue first (`uv run ai-trainer import-exercises`): with none, no candidate passes the floor, Jev isn't called, and the run measures nothing. The scenario's first attempt ended `no_card` on all 10 turns and was thrown away. #402's exercise card has no draft ID, so the script never tapped or confirmed, and history was never reached. The script now counts the exercise card as the shown card and confirms the draft the last answer shows, tested against the real templates. A sport or reading question still counts as `no_card`, because its answer runs an extraction.
  - **The router's timeout (#436, 2026-11-02).** A log naming two exercises no longer reaches the router, and its timeout is 15 s. Four runs on this scenario gave p50 1.7 s and p95 2.6 s with no timeout (ADR-0026 has the figures). G14's draft-card gap above still stands.
- Verified at the import ticket (#375), on the pinned dataset, 2026-10-31:
  - **Index function.** `unaccent` is only `STABLE`, so an index can't call it. The migration adds `exercise_name_norm(text)`, an `IMMUTABLE` wrapper with an explicit dictionary that also collapses whitespace. The name indexes use it, trigram and full-text, and `alias_norm` is a column generated from it, so a later lookup that calls the same function can't normalize differently from a stored alias.
  - **Source defects, handled in `overrides.yaml`.** Ten pairs of rows share a name after cleaning, two of them differing only in punctuation (`close grip`, `close-grip`), so one of each is skipped: 1,324 − 10 + 1 (Face Pull) = 1,315 rows. Four names hold a mis-encoded degree sign (`45в°`), one a stray `_shoulder` and one a bare `male` with no brackets; they are renamed. The import refuses two rows a person couldn't tell apart and an override naming an unknown row, so a pin bump can't add either unnoticed.
  - **Attribution.** Every source row's `attribution` is the media credit `© Gym visual`, which doesn't apply to the data. Rows store our own text naming the MIT-licensed data, the commit and the fetch date (G10), and `data/catalogue/LICENSE.exercises-dataset` keeps the notice MIT requires.
  - **Measures.** `resistance band` counts as a band, like `band`. Other equipment follows the `load_reps` rule, so a stability-ball crunch defaults to `load_reps` with its load left empty.
- **Amended at #429 (the catalogue search as a third input), owner-directed at its plan gate, 2026-11-07, and corrected after review the same day.** The owner kept the cap at `exercise_shortlist_size` and asked for the search's ranking to be fixed first, and for the acceptance criterion to be amended only if that still missed. It still missed. The figures are from the imported catalogue (1,315 exercises, English and Polish texts embedded with `qwen/qwen3-embedding-8b`), measured on this ticket's worktree on 2026-11-07 with `uv run ai-trainer-evals smoke exercise_shortlist` and `make latency-scenario sport=gym`, on the code as it ships.
  - **Shape.** `SearchedExerciseNames` (`application/exercise_candidates.py`) decorates `ExerciseNamesPort`. For a name history doesn't know it fuses two rankings of exercises by reciprocal rank at k = 60:
    - today's trigram and full-text list, read 20 deep;
    - the search, below.

    It reads the fused exercises (up to 40) in one user-scoped read (invariant 3), the entry's own measure first, and cuts at the cap. The measure order is the trigram list's own, and a search hit must not jump it: a timed plank would otherwise take a card slot from a sets-and-reps entry. Jev's question, its candidate count and `exercise_pick_threshold` are unchanged.
  - **History is still first.** A name the athlete settled is answered before any shortlist is read, so it makes no search and no embedding call.
  - **What the search reads.** `search_knowledge` filtered to `gym` and to the catalogue's own chunks, so no paper takes a slot, 20 deep per leg. All the fused passages (up to 40) are grouped by exercise, each at its best passage, before anything is cut: an exercise has an English and a Polish chunk, and the tool's cap of 10 passages would have left five exercises or fewer.
  - **The query instruction.** A typed name describes a movement, it isn't a question, so it is embedded behind its own instruction, `exercise.query` v1 (`llm/prompts/exercise.query/v1.en.md`, keyed so in `llm_calls`): "Retrieve the how-to instructions of the gym exercise whose movement matches this description". The knowledge question's instruction ranked Dumbbell Lateral Raise 23rd for "rising straight arms on both sides", and this one 5th. Five wordings were tried on four cases from one transcript, so the choice is fitted to them. The text leg is empty for the two paraphrases (`websearch_to_tsquery` needs every word), so leaving it out changes nothing there.
  - **Fallback, timeout and memory.** A search that fails, is empty, matches no exercise or runs past `EXERCISE_SEARCH_TIMEOUT_SECONDS` (default 2.5 s), and a failure to read the fused exercises, leave today's shortlist, and the log still drafts. The log line names the reason, never the typed name. The latest 256 searches are remembered by athlete and name, as IDs alone, because a name Jev isn't sure of is shortlisted twice, once to pick and once for the card, and the first run paid two embeddings per new name. An answer is kept for 10 minutes, so a catalogue re-ingest is seen within minutes. A failure is kept for 30 seconds, long enough for the card to show the shortlist Jev was shown without waiting out a second timeout, and short enough that it cannot keep a name off the search.
  - **Budget.** The search's embedding call runs on the log path, which holds no slot of its own, and a log naming N new exercises makes N calls at once. It is therefore charged to the athlete's model-call budget (ADR-0029) like a text or decision call; a refused call reads as rate limited and the shortlist falls back. The embedding calls inside a specialist's tool loop stay under that loop's one slot. Evals are not capped.
  - **Off by default** (on since #508, below). `EXERCISE_SEARCH_ENABLED` ships `false`. It needs the catalogue imported and ingested (`import-exercises`, then the `ingest` container) in the environment it is turned on in; without chunks the search finds nothing and falls back. Turning it on is the owner's step.
  - **Rank of the wanted exercise,** exercise level. "Question instruction" is a 200-deep probe; the rest is the shipped path. "Final" is the shortlist of 4 for the entry's measure:

    | Typed name | Wanted | Question instruction | `exercise.query` v1 | In the final 4 |
    |---|---|---|---|---|
    | rising straight arms on both sides | Dumbbell Lateral Raise | 23rd | 5th | no |
    | Triceps - straightening arm bent to 90 degr | Dumbbell Kickback | 22nd | beyond the 18 found | no |
    | Pull ups (`reps`) | Pull-Up | 7th | 8th | yes, with the search off too |
    | Face pull | Face Pull | 1st | 1st | yes, with the search off too |

    The kickback label is the agent's reading of the owner's wording, to be confirmed (#508). The owner corrected it at #508 to the rope pushdown, below.
  - **Smoke set.** The `live` cases pass 2 of 4 with the search off and 2 of 4 with it on: Pull-Up and Face Pull, found by trigram matching alone. A live case states the entry's measure, `reps` for "8 pull ups 3x" and `load_reps` otherwise. The two paraphrases fail either way. What the search changes is the kind of candidate: for "rising straight arms on both sides" it replaces a stability-ball crunch with Dumbbell Rear Lateral Raise and Dumbbell Raise, the lateral-raise family, but not Dumbbell Lateral Raise itself. An earlier reading of 1 of 4 against 2 of 4 entered the pull-up as a loaded lift, which sorted Pull-Up behind its weighted variants, and was wrong. ⚠ The search has not been shown to raise the pass count on these four cases; whether to turn it on is the owner's call (#508). The owner amended this criterion at review on 2026-11-07 to "measured off and on and recorded, with no regression", which 2 of 4 against 2 of 4 meets.
  - **The pool was not widened.** A pool of 25 candidates would leave the card at 4, so the athlete would not see the right exercise, and it would change Jev's inputs without recalibrating its threshold (ADR-0020, ADR-0034). Probed on 2026-11-07: even with Dumbbell Lateral Raise in a pool of 25, Jev answers "none" at 0.9.
  - **G14 holds.** Four runs of 10 gym log turns per setting, new athlete each run, `.env.example` models and timeouts, search on at 2.5 s:

    | | Search off | Search on |
    |---|---|---|
    | All 40 log turns, p50 / p95 / max | 1.46 s / 2.29 s / 4.4 s | 1.57 s / 3.51 s / 3.8 s |
    | 16 turns with a new name, p50 / p95 | 1.68 s / 4.42 s | 2.55 s / 3.79 s |
    | Embedding call, p50 / p95 / max | not made | 0.70 s / 1.86 s / 1.95 s (20 calls, one per new name, none failed) |
    | Timeouts, failures | 0, 0 | 0, 0 |

    An earlier set of runs with a 1.5 s timeout lost 2 of 22 embedding calls to it, which is why the default is 2.5 s. The scenario's USD per log turn did not change at six decimals. Percentiles are nearest rank over the pooled turns, as the scenario computes them. The run-to-run spread is wide, so read the added 0.1 to 0.9 s as a rough size.
  - **Not done** (done at #508 where noted, below). `find_exercise` (ADR-0010) still uses today's shortlist. The acceptance criteria "scores higher than before" and "lists Dumbbell Lateral Raise among its candidates" were not met on the shipped path (2 of 4 both ways; 23rd to 5th). The owner amended both on 2026-11-07 and merges the search off: the first to "measured off and on and recorded, with no regression", the second to "ranks higher than before". Raising recall, and whether the search is turned on, is #508.
- **Amended at #508 (the shortlist's recall for paraphrased names), owner-directed at its plan gate and after its measurements, 2026-11-07.** Measured on the imported catalogue (1,315 exercises, embedded with `qwen/qwen3-embedding-8b`) in the ticket's worktree, with a rank probe that is not committed, `uv run ai-trainer-evals smoke exercise_shortlist` and `make latency-scenario sport=gym`.
  - **Why the lateral raise was cut.** Even fusion interleaves the two lists, trigram 1st, search 1st, trigram 2nd, search 2nd. For a paraphrase the trigram list holds only loose look-alikes (best similarity 0.34, a stability-ball crunch), and they took half the slots. The search's 5th was the fused 6th.
  - **The search leads on a paraphrase.** When no trigram candidate reaches `EXERCISE_NAME_MATCH_FLOOR` (default 0.5), the typed name matches no catalogue name. The search's order then leads and the trigram list follows, still with the entry's own measure first. Otherwise the fusion stays as #429 set it. The trigram shortlist reports each candidate's score as `name_score`. Neither Jev nor the card reads it, so Jev's question, its candidate count and `exercise_pick_threshold` are unchanged. Paraphrases measured 0.32 and 0.34; real names 0.59 ("Triceps pushdown") to 1.0 ("Face pull"). ⚠ The floor is fitted to these few names, and moves on new field cases. Some real names also score under it, so the search leads for them too; checked at `/code-review` on the shipped code, the shortlist was better each time, search off → on: "squats" (0.38) missing → Barbell Full Squat 3rd, "deadlifts" (0.47) 4th → 1st, "benchpress" (0.45) 3rd → 1st. Two names at or above it keep the even fusion: "dips" (0.50, Ring Dips) missing → Triceps Dip 3rd, "leg press" (0.62) 2nd → 3rd.
  - **Target and equipment in each chunk.** A catalogue chunk is the instructions, then `Target: <target>. Equipment: <equipment>.` (ADR-0036's #427 amendment, as amended here). It moved the lateral raise from the search's 5th to its 3rd. The import and the worker decide "stale" by one rule (`has_changed_text`), so an import with nothing changed queues nothing; the import's SQL check had compared a chunk with the raw instructions, which would have queued both sources on every import.
  - **Rank of the wanted exercise,** exercise level, at the shipped depth of 20 per leg. "Final" is the shortlist of 4 for the entry's measure:

    | Typed name | Wanted | Search, #429 → #508 | Final 4, #429 → #508 |
    |---|---|---|---|
    | rising straight arms on both sides | Dumbbell Lateral Raise | 5th → 3rd | no → yes, 3rd |
    | Triceps - straightening arm bent to 90 degr | Cable Pushdown (With Rope Attachment) | not in the 18 found → not in the 17 | no → no |
    | Pull ups (`reps`) | Pull-Up | 8th → 6th | yes, 2nd |
    | Face pull | Face Pull | 1st | yes, 1st |

    The search alone with the new chunks, fused evenly, gave the lateral raise 5th: neither change was enough on its own.
  - **The triceps label** is the owner's, confirmed at the plan gate: the rope pushdown ("Rope Tricep Extension", "Cable Rope Triceps Pushdown"), not Dumbbell Kickback. ⚠ The paraphrase can't reach it. "Straightening arm bent" ranks every triceps extension above it, and it isn't in the search's top 60. Its common name misses for another reason: in this catalogue every "rope … tricep extension" is an overhead or lying extension, so "Rope tricep extension" lists those. "Cable rope triceps pushdown" finds it, 3rd. Curated catalogue aliases for common gym names (`exercise_aliases` with no owner, which the shortlist already reads) are the deterministic fix, and a ticket of their own, owner-directed.
  - **Smoke set.** The `live` cases pass 3 of 4 with the search on and 2 of 4 with it off, against 2 of 4 both ways at #429. The five decision cases pass both ways.
  - **On by default.** `EXERCISE_SEARCH_ENABLED` now defaults to `true`, owner-directed after these figures, and `find_exercise` shortlists through the same names as a log, uncapped because it runs inside a specialist's tool loop. An environment with no ingested catalogue still drafts: the search finds nothing and today's shortlist stands. The test suite turns it off, since its embedding call is plain HTTP that `ALLOW_MODEL_REQUESTS` doesn't guard.
  - **Not done.** A wider pool for Jev, as #429 measured it (Jev answers "none" at 0.9 with the lateral raise in a pool of 25, and the card shows 4). New `exercise.query` wordings, of which #429 tried five. New field cases: the owner asked to fix these four first.
  - **G14 holds.** One run of 10 gym log turns per setting, a new athlete each run, `.env.example` models and timeouts:

    | | Search off | Search on |
    |---|---|---|
    | 10 log turns, p50 / p95 | 1.4 s / 1.8 s | 1.5 s / 3.7 s |
    | Timeouts, failures | 0, 0 | 0, 0 |

    The slowest turn with the search on was a new name whose `gym.extract` took 2.3 s; its embedding call took 0.8 s. The embedding upstreams' tail is long: p90 5.7 to 8.0 s and p99 17 to 19 s on Nebius and DeepInfra over 30 minutes (`list-model-endpoints`, 2026-11-07), so one probe call timed out at 10 s. On the log path the 2.5 s timeout cuts such a call and today's shortlist stands.
