# Research — Exercise media sources

| | |
|---|---|
| Ticket | #416 |
| Read on | 2026-11-02 |
| Feeds | ADR-0031 (media links); the M2m "Show me" milestone and its licensed-media-source ticket (PRD 0010 F24) |

This document decides nothing. It compares where exercise pictures could come from, so the owner can pick one and M2m can be ticketed. Reads were free: no paid model call was made and no athlete data left the project.

## Questions

1. Which open image sets could supply F24 media, and under what licence?
2. What would a Gym visual licence need?
3. What are the rights and cost of Claude-written SVGs?
4. What are the rights and cost of an image model's output?
5. How many of the exercises in the first gym session does each option cover?
6. What is the form-accuracy risk of each option, as the owner sees it?

## Findings

### 1. Open image sets

**`free-exercise-db`** (head `f00c92c`, 876 exercises, 2 images each).

- The repository is [Unlicense](https://github.com/yuhonas/free-exercise-db/blob/main/LICENSE.md) (read 2026-11-02).
- Its maintainer wrote that he has "no idea where the images are from or if they are royalty free so usage would be at your own risk" ([issue #2](https://github.com/yuhonas/free-exercise-db/issues/2), read 2026-11-02).
- The upstream project it is reworked from says the images "have been scrapped off the internet, therefore l do not own the copy right for these images and would advise against using them in comercial projects" ([wrkout/exercises.json CONTRIBUTING](https://github.com/wrkout/exercises.json/blob/master/CONTRIBUTING.md), read 2026-11-02).
- A reverse image search by an issue commenter names bodybuilding.com as one source and judges the status "clearly infringing". That is a commenter's claim, not a source ruling ([wrkout/exercises.json #305](https://github.com/wrkout/exercises.json/issues/305), read 2026-11-02). It is recorded as a reason for caution, not as proof.
- Conclusion for this document: the Unlicense covers the repository's own authorship and does not give a licence to images the maintainer did not make. The images carry no usable licence.

**`wger`** (`master` at `dde7407`; 383 images on 279 exercises through its public API).

- Application code is AGPL-3.0-or-later, and "Exercise/Ingredient Data: Creative Commons (see individual entries)" ([wger README](https://github.com/wger-project/wger/blob/master/README.md), read 2026-11-02).
- Each image has its own licence, author and "AI generated" flag ([`image.py`](https://github.com/wger-project/wger/blob/dde7407/wger/exercises/models/image.py), read 2026-11-02).
- The licence list on the API holds five: CC BY-SA 3, CC BY-SA 4, CC BY 4, CC0 and ODbL ([`/api/v2/license/`](https://wger.de/api/v2/license/), read 2026-11-02).
- The image listing, read 2026-11-02 ([`/api/v2/exerciseimage/`](https://wger.de/api/v2/exerciseimage/)): 295 images are CC BY-SA 4, 88 are CC BY-SA 3, and none carries another licence. By style, 270 are photos, 100 are line art, 6 are 3D and 7 are other. 46 are flagged AI-generated. 104 have an empty author field.
- CC BY-SA means attribution plus ShareAlike. The Creative Commons FAQ says all CC licences allow licensed material in collections, that the collection's own licence "does not change the license applicable to the original material", and that ShareAlike applies when a use is an adaptation, which "depends primarily on the applicable copyright law" ([CC FAQ](https://creativecommons.org/faq/), read 2026-11-02).
- What the sources do not settle: whether showing an unaltered picture inside the app is an adaptation under Polish and EU law, and whether resizing or cropping it is. See Open questions.

**Other open sets.** Not searched. Two sets were named in the ticket and both are answered above, and the ticket asked for others only if these fell short. They did fall short on rights for `free-exercise-db` and on attribution gaps for `wger`, so a wider search is an Open question.

### 2. Gym visual

- The catalogue ADR-0031 already imports ([`hasaneyldrm/exercises-dataset`](https://github.com/hasaneyldrm/exercises-dataset), 1,324 exercises at `7455efa`) licenses its data and instruction text under MIT. Its media is © Gym visual and excluded from that licence ([LICENSE](https://github.com/hasaneyldrm/exercises-dataset/blob/main/LICENSE), read 2026-11-02).
- The repository holds the media "with the rights holder's separate written permission", at 180×180 only, with the attribution "© Gym visual — https://gymvisual.com/". Reusing it "is governed by Gym visual's Terms & Conditions" and the repository "does not grant you any rights to the media" ([NOTICE.md](https://github.com/hasaneyldrm/exercises-dataset/blob/main/NOTICE.md), read 2026-11-02).
- The terms page ([gymvisual.com terms](https://gymvisual.com/content/3-terms-and-conditions-of-use)) and the site root both returned HTTP 403 to two automated fetches on 2026-11-02. Their terms, price and app-use rules are not read. See Open questions.
- Each record in the dataset links one `gif_url` and one `image` and carries the attribution string (`gif_url` and `image` fields of `data/exercises.json` at `7455efa`, read 2026-11-02). Linking media to the catalogue exercise therefore needs no matching step.

### 3. Claude-written SVGs

- Through the API, Anthropic's commercial terms say the customer "owns its Outputs", and Anthropic "assigns to Customer its right, title and interest (if any) in and to Outputs" ([Commercial Terms](https://www.anthropic.com/legal/commercial-terms), section B, effective 2025-06-17, read 2026-11-02). The words "(if any)" mean the assignment passes on whatever rights exist.
- The consumer terms assign Outputs the same way, and also say "you will not use our Services for any commercial or business purposes" ([Consumer Terms](https://www.anthropic.com/legal/consumer-terms), sections 4 and 11, effective 2025-10-08, read 2026-11-02). The page says the Commercial Terms govern API keys and the Console, and not Claude.ai or Claude Pro use.
- Neither page mentions images or SVG, and neither says whether such output is copyrightable.
- An SVG is text, so it needs no storage service beyond what `media_assets` already plans and no attribution to a third party. Its cost is the tokens to write it, which was not measured here because writing the samples was part of this session and no per-SVG token count was kept.

### 4. An image model

- Prices as OpenRouter lists them, from `list-models` on 2026-11-02, for models whose output includes images: `google/gemini-3-pro-image`, `google/gemini-3.1-flash-image`, `google/gemini-2.5-flash-image`, `openai/gpt-5-image` and `openai/gpt-5-image-mini`, plus preview variants of two Gemini models. The `image_output` figures it prints are of the order of $0.00003 to $0.00012 per image. Their unit is not documented in the result, so they are not turned into a per-exercise cost here.
- Google's Gemini API terms: "Google won't claim ownership over that content", Google "may generate the same or similar content for others", and use of generated content "may require the provision of attribution to your users" ([Gemini API additional terms](https://ai.google.dev/gemini-api/terms), effective 2026-03-23, read 2026-11-02). The page text read has no rule on watermarks.
- OpenAI's terms returned HTTP 403 to an automated fetch ([OpenAI terms of use](https://openai.com/policies/terms-of-use/), 2026-11-02) and are not read.
- wger itself carries an "AI generated" flag and shows 46 such images ([wger API](https://wger.de/api/v2/exerciseimage/), 2026-11-02), which shows the open-set maintainers treat provenance as something to record.
- Whether purely AI-generated pictures can be copyrighted is not settled by any page read. The US Copyright Office report on copyrightability was fetched as a PDF the tools here could not render, and its conclusion is not read.
- No image was generated, so form accuracy is judged from the option's nature only (finding 6).

### 5. Coverage on the first gym session

Method: the six exercises in the [first gym session](../../evals/field/gym/2026-10-28-first-gym-session.md) are pull-ups, bench press, face pull, dumbbell lateral raise ("two hantles … rising straight arms on both sides"), triceps extension ("straightening arm bent to 90 degr") and anti-rotation with a band. Each option's exercise list was matched by name with a case-insensitive pattern (for example `pull.?up|chin.?up`) and the hits read by eye. A hit means an exercise of that name exists in the set and, for `wger`, has at least one image. It does not mean the picture is the right variant. The scripts are in this ticket's session only; the patterns are given above so the check can be repeated.

| Exercise | Gym visual (`7455efa`, 1,324) | `wger` (`dde7407`, 279 with images) | `free-exercise-db` (`f00c92c`, 876) |
|---|---|---|---|
| Pull-ups | exact: `pull-up`, `chin-up` | exact: Pull-ups, Chin Up | family (9 hits, e.g. Pullups, Chin-Up) |
| Bench press | exact: `barbell bench press`, `dumbbell bench press` | exact: Bench Press | family (21 hits) |
| Face pull | **none** (0 hits) | wrong variant only: Dumbbell Bent Over Face Pull, and a band one | exact: Face Pull |
| Dumbbell lateral raise | exact: `dumbbell lateral raise` | name only: Lateral Raises (dumbbell not in the name) | family (8 hits, e.g. Side Lateral Raise) |
| Triceps extension | family (49 hits); the exact movement is open | family: Barbell, Overhead, Cable | family (32 hits) |
| Anti-rotation (Pallof) | family: `band horizontal pallof press`, `band vertical pallof press` | exact: Pallof Press | exact: Pallof Press |
| Clear hit | 4 of 6, plus 2 families | 3 of 6, plus 3 weaker | 2 of 6, plus 4 families |

"Family" means several variants exist and picking the one the athlete did needs a choice, which is the work the exercise card (F23) already does. ADR-0031 records that "Face pull" is not in the Gym visual dataset, and this measure agrees ([ADR-0031 Consequences](../adr/0031-exercise-catalogue-and-name-resolution.md)).

Six exercises are a smoke check on one athlete's one session, not a coverage rate for the catalogue.

### 6. Options as the owner sees them

Five samples, each with the open-set picture, a Claude-written SVG, and the two options that could not be shown, are in [Exercise media samples](https://claude.ai/design/p/f96f5229-4162-4b6c-a704-641a8024066a?file=Exercise+media+samples.html) (Claude Design, read 2026-11-02). The exercises are pull-ups, bench press, face pull, dumbbell lateral raise and Pallof press. Form-accuracy risk per option:

| Option | Form-accuracy risk |
|---|---|
| `wger` image | Depends on the picture. Line-art pairs by a named author (bench press, lateral raise) show start and end. A single photo (pull-ups) does not. Name matches can pick the wrong variant (face pull is the dumbbell bent-over row-like move). Some pictures are AI-flagged (two of three for Pallof press). |
| Claude-written SVG | Stick figures draw a plausible shape and can hide errors: grip, scapular position, elbow flare, and rotation resistance cannot be shown reliably. Every sample was drawn once and none was reviewed by a coach. |
| Image model | Not generated. Assumed highest, because a model can draw plausible bodies with the wrong joint angles. This is a judgment from the option's nature, not a measurement, and it is the first thing a paid sample would test. |
| Gym visual | Not shown because their terms are unread. A catalogue with one consistent animated style is likely lowest, but each match still needs a name check, as for any option. |

## Comparison

| Option | Licence | Attribution needed | Catalogue coverage (first session) | Cost |
|---|---|---|---|---|
| `free-exercise-db` images | Repository Unlicense; the images' rights are unknown and likely infringing (finding 1) | Not applicable: no licence to attribute under | 2 clear + 4 families of 6, 876 exercises | Free to copy; legal exposure unquantified |
| `wger` images | Per image: CC BY-SA 4 (295) or CC BY-SA 3 (88), by named authors | Yes: author and licence link per image; 104 have no author listed | 3 clear + 3 weaker of 6, 279 exercises with images | Free; storage only |
| Gym visual | © Gym visual, own licence required | Yes: "© Gym visual — https://gymvisual.com/" | 4 clear + 2 families of 6, 1,324 exercises, already matched to ADR-0031's catalogue | Not read; Open question |
| Claude-written SVG | Outputs assigned to the customer under the Commercial Terms; copyrightability not settled | No third party; our own credit line is a choice | Any exercise we write one for | Tokens per SVG, not measured |
| Image model (per provider) | Gemini: Google claims no ownership; OpenAI: not read; others: not read | Gemini: "may require" attribution to users | Any exercise we generate | Provider prices as listed on OpenRouter, unit unclear; none run |

## Recommendations for this project

These are inputs for the owner and for the M2m ticket. None of them decides anything, and none contradicts ADR-0031: it already says no media is imported "until a source with a license we hold is chosen".

- Treat `free-exercise-db` images as unusable. Its text and JSON may still be useful as data, which is outside this ticket.
- Before choosing, read Gym visual's terms in a browser and ask them for a quote. It is the only option that covers 4 of 6 first-session exercises, and it has the same ids as the catalogue ADR-0031 already imports. If the price or app-use rule rules it out, the answer is already in hand.
- Consider `wger` as the free fallback only if the project can meet CC BY-SA attribution (author, licence link) per image, and only after deciding what the 104 images with no author, and the 46 AI-flagged ones, mean for G10's "shown with its source". The admin review step in F24 is where such a picture would be skipped.
- An SVG set is the only option with no rights risk from a third party, and it can cover face pull. Its limit is form accuracy, which a human reviewer must judge. The samples let the owner judge whether the style is acceptable at all.
- Run a small image-model sample only if the owner asks. It is the one paid step and the least known on rights.
- If the owner picks an option that changes ADR-0031's media tables or the PRD's F24 wording, that change goes through `/update-docs`.

## Open questions

- **Gym visual terms, price and app use.** Both pages returned HTTP 403 to automated fetches on 2026-11-02. Settled by the owner reading https://gymvisual.com/content/3-terms-and-conditions-of-use in a browser, or by asking Gym visual for a quote.
- **Other open image sets.** Only `free-exercise-db` and `wger` were checked. Settled by a wider search if the owner wants an alternative to Gym visual.
- **CC BY-SA and in-app use.** Whether an unaltered or resized `wger` picture in the app is an adaptation under Polish and EU law is not settled by the FAQ read. Settled by a lawyer, or by choosing another option.
- **`wger` images with no author.** 104 of 383 have an empty author field. How to attribute them is not answered by any source read.
- **OpenAI's terms on output.** HTTP 403 on 2026-11-02. Settled by reading them in a browser, or by dropping OpenAI from the shortlist.
- **Copyrightability of AI output.** The US Copyright Office report on copyrightability was not read. It is a US view, and the project is Europe first. Settled by reading that report and an EU source, and it bears on whether an SVG or an image-model picture can be owned at all.
- **Which Anthropic terms govern a Claude-written SVG used in the product.** The consumer terms bar commercial use and the commercial terms assign outputs. Which applies depends on the account used to write the SVG. Settled by the owner.
- **Per-image cost of the image models.** OpenRouter's `image_output` unit is not stated in the listing. Settled by `get-model` on a chosen model, or by the one paid sample.
- **Form accuracy of an image model.** Not measured. Settled by a sample the owner asks for.
- **Whether the catalogue's `wger` matches are the right variants.** Hits were matched by name only. Settled by an admin looking at each picture, which F24 already requires.

## Sources

| Source | Kind | Version or date read |
|---|---|---|
| [`yuhonas/free-exercise-db`](https://github.com/yuhonas/free-exercise-db) (LICENSE.md, README, issue #2, dist/exercises.json) | Upstream code | head `f00c92c`, 2026-11-02 |
| [`wrkout/exercises.json`](https://github.com/wrkout/exercises.json) (CONTRIBUTING.md, issue #305) | Upstream code | 2026-11-02 |
| [`wger-project/wger`](https://github.com/wger-project/wger) (README, `image.py`) | Upstream code | `master` at `dde7407`, 2026-11-02 |
| [wger public API](https://wger.de/api/v2/exerciseimage/) (images, licences) | Upstream data | 2026-11-02 |
| [`hasaneyldrm/exercises-dataset`](https://github.com/hasaneyldrm/exercises-dataset) (LICENSE, NOTICE.md, `data/exercises.json`) | Upstream code | `7455efa`, 2026-11-02 |
| [Creative Commons FAQ](https://creativecommons.org/faq/) | Official documentation | page dated 2025-05-13, read 2026-11-02 |
| [Anthropic Commercial Terms](https://www.anthropic.com/legal/commercial-terms) | Provider's own terms | effective 2025-06-17, read 2026-11-02 |
| [Anthropic Consumer Terms](https://www.anthropic.com/legal/consumer-terms) | Provider's own terms | effective 2025-10-08, read 2026-11-02 |
| [Gemini API additional terms](https://ai.google.dev/gemini-api/terms) | Provider's own terms | effective 2026-03-23, read 2026-11-02 |
| OpenRouter `list-models` (image-output models and listed prices) | OpenRouter MCP | 2026-11-02 |
| [Exercise media samples](https://claude.ai/design/p/f96f5229-4162-4b6c-a704-641a8024066a?file=Exercise+media+samples.html) | Claude Design project, this ticket | 2026-11-02 |
| [First gym session transcript](../../evals/field/gym/2026-10-28-first-gym-session.md) | Project data | 2026-10-28 |
