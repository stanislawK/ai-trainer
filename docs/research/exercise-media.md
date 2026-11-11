# Research — Exercise media sources

| | |
|---|---|
| Ticket | #416 |
| Read on | 2026-11-02; finding 7 added 2026-11-07; findings 9 and 10 added 2026-11-11 |
| Feeds | ADR-0031 (media links); the M2m "Show me" milestone and its licensed-media-source ticket (PRD 0010 F24). Used at #514 (2026-11-07): the pick is recorded in ADR-0031, "First media source (#514)". Used at #524 (2026-11-08): the fallback pick is recorded in ADR-0031, "Fallback media source (#524)" |

This document decides nothing. It compares where exercise pictures could come from, so the owner can pick one and M2m can be ticketed. Reads were free: no paid model call was made and no athlete data left the project.

> Added 2026-11-07: [PRD 0012](../prd/0012-ai-training-companion.md) G16 and ADR-0031's amendment of that date now admit embedding media whose owner published it freely without stating a licence. Finding 7 records the case law that rests on. Findings 1–6 are unchanged, and where they call a source unusable to copy, that still holds.

## Questions

1. Which open image sets could supply F24 media, and under what licence?
2. What would a Gym visual licence need?
3. What are the rights and cost of Claude-written SVGs?
4. What are the rights and cost of an image model's output?
5. How many of the exercises in the first gym session does each option cover?
6. What is the form-accuracy risk of each option, as the owner sees it?
7. Under EU law, when may the app link to or embed media it holds no licence for? (added 2026-11-07)
8. Why does the picture call fail through our command when the MCP drew with the same model? (added 2026-11-11, #544; answered in finding 10)

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
- Each record in the dataset links one `gif_url` and one `image` and carries the attribution string (`gif_url` and `image` fields of `data/exercises.json` at `7455efa`, read 2026-11-02). Media for this dataset's exercises is therefore already attached to the same rows ADR-0031 imports, so no matching between two datasets is needed. A picture can still be the wrong variant, which F24's admin review checks.

### 3. Claude-written SVGs

- Through the API, Anthropic's commercial terms say the customer "owns its Outputs", and Anthropic "assigns to Customer its right, title and interest (if any) in and to Outputs" ([Commercial Terms](https://www.anthropic.com/legal/commercial-terms), section B, effective 2025-06-17, read 2026-11-02). The words "(if any)" mean the assignment passes on whatever rights exist.
- The consumer terms assign Outputs the same way, and also say "you will not use our Services for any commercial or business purposes" ([Consumer Terms](https://www.anthropic.com/legal/consumer-terms), sections 4 and 11, effective 2025-10-08, read 2026-11-02). The page says the Commercial Terms govern API keys and the Console, and not Claude.ai or Claude Pro use.
- Neither page mentions images or SVG, and neither says whether such output is copyrightable.
- ADR-0031's `media_assets.kind` already allows `drawing` ([ADR-0031, Media links](../adr/0031-exercise-catalogue-and-name-resolution.md), read 2026-11-02), so an SVG needs no new kind. Whether it needs the media bucket or could sit in the table is not decided anywhere read; see Open questions.
- The cost of an SVG is the tokens to write it. That was not measured: no per-SVG token count was kept while the samples were written, so it is an Open question, not a figure.

### 4. An image model

- Prices as OpenRouter lists them, from `list-models` on 2026-11-02, for models whose output includes images: `google/gemini-3-pro-image`, `google/gemini-3.1-flash-image`, `google/gemini-2.5-flash-image`, `openai/gpt-5-image` and `openai/gpt-5-image-mini`, plus preview variants of two Gemini models. The `image_output` field it prints holds figures between 0.00003 and 0.00012, labelled `/image`. The result does not say what that unit is, and such small amounts suggest it is not a price per picture, so no per-exercise cost is derived here.
- Google's Gemini API terms: "Google won't claim ownership over that content", Google "may generate the same or similar content for others", and use of generated content "may require the provision of attribution to your users" ([Gemini API additional terms](https://ai.google.dev/gemini-api/terms), effective 2026-03-23, read 2026-11-02). The page text read has no rule on watermarks.
- OpenAI's terms returned HTTP 403 to an automated fetch ([OpenAI terms of use](https://openai.com/policies/terms-of-use/), 2026-11-02) and are not read.
- wger itself carries an "AI generated" flag and shows 46 such images ([wger API](https://wger.de/api/v2/exerciseimage/), 2026-11-02), which shows the open-set maintainers treat provenance as something to record.
- Whether purely AI-generated pictures can be copyrighted is not settled by any page read. The US Copyright Office report on copyrightability was fetched as a PDF the tools here could not render, and its conclusion is not read.
- No image was generated at the time of writing, so form accuracy was judged from the option's nature only (finding 6). Generated samples and real costs for two of the models are in finding 8 (#523).

### 5. Coverage on the first gym session

Method: the six exercises in the [first gym session](../../evals/field/gym/2026-10-28-first-gym-session.md) are pull-ups, bench press, face pull, dumbbell lateral raise ("two hantles … rising straight arms on both sides"), triceps extension ("straightening arm bent to 90 degr") and anti-rotation with a band. Each option's exercise list was matched by name with these case-insensitive patterns, and the hits read by eye: `pull.?up|chin.?up`, `bench press`, `face.?pull`, `lateral raise|side raise`, `triceps? .*(extension|kickback)|(extension|kickback).*triceps?|skull` (for `wger`: `tricep.*(extension|kickback)|(extension|kickback).*tricep|skull|french press`), and `pallof|anti.?rotation`. The lists read were [`data/exercises.json` at `7455efa`](https://github.com/hasaneyldrm/exercises-dataset/blob/7455efa/data/exercises.json), [`dist/exercises.json` at `f00c92c`](https://github.com/yuhonas/free-exercise-db/blob/f00c92c7dcf1216a928a52c3706c7ce8e2f71ed5/dist/exercises.json), and the `wger` image listing and per-exercise names from its [public API](https://wger.de/api/v2/exerciseimage/) (read 2026-11-02, counts will drift). A hit means an exercise of that name exists in the set and, for `wger`, has at least one image. It does not mean the picture is the right variant. The matching scripts were not committed, so the counts can be reproduced only by repeating these patterns.

| Exercise | Gym visual (`7455efa`, 1,324) | `wger` (`dde7407`, 279 with images) | `free-exercise-db` (`f00c92c`, 876) |
|---|---|---|---|
| Pull-ups | exact: `pull-up`, `chin-up` | exact: Pull-ups, Chin Up | family (9 hits, e.g. Pullups, Chin-Up) |
| Bench press | exact: `barbell bench press`, `dumbbell bench press` | exact: Bench Press | family (21 hits) |
| Face pull | **none** (0 hits) | wrong variant only: Dumbbell Bent Over Face Pull, and a band one | exact: Face Pull |
| Dumbbell lateral raise | exact: `dumbbell lateral raise` | name only: Lateral Raises (dumbbell not in the name) | family (8 hits, e.g. Side Lateral Raise) |
| Triceps extension | family (49 hits); the exact movement is open | family: Barbell, Overhead, Cable | family (32 hits) |
| Anti-rotation (Pallof) | family: `band horizontal pallof press`, `band vertical pallof press` | exact: Pallof Press | exact: Pallof Press |
| Clear hit | 3 of 6, plus 2 families, 1 none | 3 of 6, plus 3 weaker | 2 of 6, plus 4 families |

"Exact" means the set has an exercise under the plain name; "family" and "weaker" are my judgment of how close the hits are, and the patterns are broad (for example `skull` and `kickback` both count toward triceps). "Family" means several variants exist and picking the one the athlete did needs a choice, which is the work the exercise card (F23) already does. ADR-0031 records that "Face pull" is not in the Gym visual dataset, and this measure agrees ([ADR-0031 Consequences](../adr/0031-exercise-catalogue-and-name-resolution.md)).

Six exercises are a smoke check on one athlete's one session, not a coverage rate for the catalogue.

### 6. Options as the owner sees them

Five samples are in one Claude Design page, [Exercise media samples](https://claude.ai/design/p/f96f5229-4162-4b6c-a704-641a8024066a?file=Exercise+media+samples.html) (Claude Design, read 2026-11-02). The exercises are pull-ups, bench press, face pull, dumbbell lateral raise and Pallof press, one section each. Each section shows the `wger` picture and a Claude-written SVG for real. The image-model and Gym visual cards are in the page too, but only as empty cards that say why no picture is shown. Form-accuracy risk per option:

| Option | Form-accuracy risk |
|---|---|
| `wger` image | Depends on the picture. The line-art pairs by Everkinetic (bench press, lateral raise) show start and end. A single photo (pull-ups) does not. A name match can pick the wrong variant: the face pull hit is "Dumbbell Bent Over Face Pull", not a cable face pull. Some pictures are AI-flagged: two of the three Pallof press pictures. These details come from the API records for [bench press](https://wger.de/api/v2/exerciseimage/?exercise=73), [lateral raise](https://wger.de/api/v2/exerciseimage/?exercise=348), [pull-ups](https://wger.de/api/v2/exerciseimage/?exercise=475), [face pull](https://wger.de/api/v2/exerciseimage/?exercise=1639) and [Pallof press](https://wger.de/api/v2/exerciseimage/?exercise=1194) (read 2026-11-02). |
| Claude-written SVG | Stick figures draw a plausible shape and can hide errors: grip, scapular position, elbow flare, and rotation resistance cannot be shown reliably. Every sample was drawn once and none was reviewed by a coach. |
| Image model | Not generated, so unknown. No source read measures it. See Open questions. |
| Gym visual | Not shown because their terms are unread, so unknown. Its dataset links one picture and one animation to each exercise, and each match still needs a name check, as for any option. |

### 7. Linking and embedding under EU law

Read on EUR-Lex, 2026-11-07. The operative parts were read verbatim; *BestWater* has no English text on EUR-Lex and was read in French.

- **Links.** *Svensson* (C-466/12, 13 February 2014): "the provision on a website of clickable links to works freely available on another website" "does not constitute an 'act of communication to the public'". The reasoning is that there is no new public (paras 24–28). Para 31 makes an exception: a link that gets around a restriction confining access to the first site's subscribers needs authorisation.
- **Framing.** *BestWater* (C-348/13, order of 21 October 2014): a freely available work embedded on another site by framing is not a communication to the public, as long as it reaches no new public and uses no different technical means. The operative part does not mention consent; the reasoning assumes the work was published with the right holder's authorisation (paras 16, 18).
- **Knowledge.** *GS Media* (C-160/15, 8 September 2016): for links to a work published without consent, it matters whether they were "provided without the pursuit of financial gain by a person who did not know or could not reasonably have known the illegal nature of the publication". For a profit-making linker, that knowledge "must be presumed" (para 51). For a non-profit linker, lack of knowledge is a factor to take into account, not a presumption (para 47). A linker who knew or ought to have known, for example after a notice from the right holder, communicates to the public (para 49).
- **Measures against framing.** *VG Bild-Kunst* (C-392/19, Grand Chamber, 9 March 2021): framing a freely accessible work "where that embedding circumvents measures adopted or imposed by that copyright holder to provide protection from framing, constitutes a communication to the public".

What this means for the options above:
- Embedding is safest for media its owner published, such as a creator's own channel or site.
- It does not help for `free-exercise-db`'s images (finding 1): we have reason to think they are copies, and *GS Media* para 49 puts that knowledge against us.
- None of the four cases addresses a person's right to their own image.


| Option | Licence | Attribution needed | Catalogue coverage (first session) | Cost |
|---|---|---|---|---|
| `free-exercise-db` images | Repository Unlicense; the images' rights are unknown and likely infringing (finding 1) | Not applicable: no licence to attribute under | 2 clear + 4 families of 6, 876 exercises | Free to copy; legal exposure unquantified |
| `wger` images | Per image: CC BY-SA 4 (295) or CC BY-SA 3 (88), by named authors | Yes: author and licence link per image; 104 have no author listed | 3 clear + 3 weaker of 6, 279 exercises with images | Free; storage only |
| Gym visual | © Gym visual, own licence required | Yes: "© Gym visual — https://gymvisual.com/" | 3 clear + 2 families + 1 none of 6, 1,324 exercises, the same rows as ADR-0031's catalogue; media is 180×180 only | Not read; Open question |
| Claude-written SVG | Outputs assigned to the customer under the Commercial Terms (API, Console); the Consumer Terms bar commercial use, so the account used to write an SVG matters (the samples were written in a Claude Code session); copyrightability not settled | No third party; our own credit line is a choice | Any exercise we write one for | Tokens per SVG, not measured |
| Image model (per provider) | Gemini: Google claims no ownership; OpenAI: not read; others: not read | Gemini: "may require" attribution to users | Any exercise we generate | Measured 2026-11-08 (finding 8): Recraft vector $0.08 and Gemini 3.1 Flash Lite Image $0.0336 per picture; the catalogue's `image_output` figures are not prices; OpenAI's model not run. Recraft's ownership terms found cover its own paid plan, and the Developer Terms for API use are unread (finding 8) |

### 8. Generated pictures, sampled (#523)

Run on 2026-11-08, with the owner's yes and a spend cap of $1 in the session. Every prompt named only the exercise ("<exercise> exercise, instructional line illustration showing the start and end position", 1:1) and carried no athlete data. The exercises are the six of finding 5. The tool used was the OpenRouter MCP `generate-image`; its `get-current-key` usage rose from $0 to $0.681621 over the run, which matches the sum of the per-call costs the tool printed ($0.681600) to within $0.00003, as rounding of the printed figures would explain.

| Model | Pictures | Cost per picture | Format and size |
|---|---|---|---|
| `recraft/recraft-v4.1-vector` | 6 of 6 | $0.08 each (tool-printed, 2026-11-08) | SVG, 2048×2048 viewBox shown at 1024; 66.9 to 163.8 KiB (about 4,200 tokens per call). Each file embeds a C2PA manifest in `<metadata>`, about 17 KiB |
| `google/gemini-3.1-flash-lite-image` | 6 of 6 | $0.0336 each (tool-printed, 2026-11-08) | JPEG, 1024×1024; 154.0 to 264.8 KiB |
| `openai/gpt-image-2.6-sunburst` | 0 of 6 | not run | not run |

- **Cost source.** The tool returned no generation ID, so `get-generation` could not be called, and the cost is the figure printed with each call, cross-checked against the key's usage total. The owner agreed to this on 2026-11-08. The catalogue's `image_output` figures (for example $0.0000191616766467066/image for the Recraft model, $0.00003/image for the other two) are not prices: the real cost was about 4,200 times higher for Recraft and about 1,100 times higher for Gemini.
- **Skipped: `openai/gpt-image-2.6-sunburst`.** The tool has no `output_format` option, so the `gif` request could not be made. Under the ticket's stop rule nothing was generated for this model, and its cost, file size, frame count and duration remain unknown. The owner chose to skip it rather than run one default-format probe. A rerun needs a way to send `output_format: gif` to `/api/v1/images`.
- **Form accuracy** is for the owner to judge on the [samples page](https://claude.ai/artifact/BgoG9dnEuAkhsxVU9sJwut), one row per exercise. Two observations while generating, not a verdict: the Gemini pictures carry printed labels, some wrong ("Babows", "Spotter's Arms" on the bench press, "Hanging Body Body" on the pull-up), and the Gemini face pull ends with the rope across the face and elbows flared. The Recraft SVGs were not assessed for errors in this run, so no comparison of the two models' accuracy follows from this finding. The samples page is a private Artifact visible to its owner and anyone they share it with, and the files are not committed; if it is deleted, these observations cannot be re-checked.
- **Where the samples live.** The page is a private Artifact on claude.ai, not a Claude Design page, because Claude Design accepts file data only inline and the 12 files total about 0.9 MB after stripping the C2PA block from the SVGs and shrinking the JPEGs to 512 px. The SVGs on that page have the C2PA block removed, so their sizes there are smaller than in the table. The files themselves are not committed.

Output terms, read 2026-11-08:

- **Recraft.** Terms effective 21 October 2026 ([recraft.ai/terms](https://www.recraft.ai/terms)). Paid subscription: "You own all Assets you create with the Services", and Recraft "hereby assigns to you all copyright rights it may have in the Assets". Free tier: "Free Tier Assets are owned by Recraft", and "no commercial use of Free Tier Assets is permitted". No attribution requirement. Section 8.2 says the user will not remove or alter machine-readable metadata or watermarks Recraft applies, which is relevant to the C2PA block above. Assets may not be used to train AI models. Section 7.3 sends API and MCP access to separate Developer Terms, which were not read, so what applies to a key used through OpenRouter is **unread**. Reason: the Terms page text returned by the fetch tool named the Developer Terms without giving their text, and no separate URL for them was followed in this run.
- **Google.** [Gemini API additional terms](https://aistudio.google.com/docs/terms) (the old ai.google.dev URL redirects there), effective 23 March 2026: "Google won't claim ownership over that content", and use of generated content "may require the provision of attribution to your users when returned as part of an API call". No mention of watermarks or SynthID on that page.
- **OpenAI.** **Unread.** `openai.com/policies/service-terms/` and `openai.com/policies/row-terms-of-use/` returned HTTP 403 to an automated fetch on 2026-11-08, as `terms-of-use` did on 2026-11-02 (finding 4). Nothing is claimed about output ownership for `gpt-image-2.6-sunburst`, which was also not run.
- Whether such pictures can be copyrighted at all is still not settled by any page read (finding 4).

### 9. Re-sample with generated briefs (#534)

Run on 2026-11-11 with the owner's yes, in the ticket's worktree database, as the dev-session admin (`ADMIN_EMAILS` set on the command line only). The command was `ai-trainer generate-exercise-picture "<name>" --out <dir>`, once per exercise, with `IMAGE_GENERATION_SPEND_CAP_USD=0.12` per run inside the owner's cap of **$0.50** for the whole re-sample. The brief model was `z-ai/glm-5.3-flash` and the picture model `google/gemini-3.1-flash-lite-image`, the same picture model as finding 8. No athlete data was sent. All six runs wrote a picture and a brief on the first try.

The exercises are finding 5's six. The catalogue has no plain "Triceps Extension" or "Pallof Press" (`import-exercises`, 1,315 rows), so I picked the closest catalogue exercises: "Dumbbell Seated Triceps Extension" (overhead, the nearest to finding 5's "straightening arm bent to 90 degr", a judgment) and "Band Horizontal Pallof Press". The others resolved by name: "Pull-Up", "Barbell Bench Press", "Face Pull", "Dumbbell Lateral Raise". Finding 8 drew from the plain names, so two rows compare a catalogue variant with a plain name.

| Exercise (catalogue name) | Cost, command line | Printed text | Brief used (`image_prompt` of its `.brief.json`) |
|---|---|---|---|
| Pull-Up | $0.033950 | no | "One figure on a plain background demonstrating a pull-up. Start position: the person hangs from an overhead pull-up bar, gripping it with palms facing away from the body, arms fully extended straight, body hanging vertically below the bar. End position: the person has pulled their body up towards the bar, elbows bent, chest close to the bar, shoulder blades squeezed together, core engaged. Show the figure clearly in both positions. No text, letters, numbers, labels or arrows in the picture." |
| Barbell Bench Press | $0.033993 | no | "One figure on a plain background demonstrating the barbell bench press. Start position: the person lies flat on their back on a bench, feet flat on the ground and back pressed against the bench, gripping a barbell with an overhand grip slightly wider than shoulder-width, arms fully extended holding the barbell directly above the chest. End position: the same person in the same lying position, barbell lowered to touch the chest, elbows tucked in and bent at the sides. Show both positions clearly. No text, letters, numbers, labels or arrows in the picture." |
| Face Pull | $0.033944 | no | "Fitness instruction illustration of the Face Pull exercise, showing one figure twice on a plain background. Start position: the person stands facing a cable pulley machine set at about face height, gripping one end of the rope attachment in each hand with palms facing each other, arms fully straight, body stepped back so the cable is taut and the weight is lifted off the stack. End position: the same person has pulled the rope towards their face, elbows kept high and out to the sides, hands spread apart so they finish beside the ears, shoulder blades squeezed together, cable taut. Clean, simple instructional style, no text, letters, numbers, labels or arrows in the picture." |
| Dumbbell Lateral Raise | $0.033981 | no | "One figure shown on a plain background demonstrating the dumbbell lateral raise. Start position: standing upright, feet shoulder-width apart, back straight, core engaged, a dumbbell in each hand with arms hanging down at the sides, palms facing the body, slight bend in the elbows. End position: same upright stance, arms raised out to the sides until parallel to the floor at shoulder height, dumbbells in each hand, slight bend in the elbows. Clean fitness illustration style, no text, letters, numbers, labels or arrows in the picture." |
| Dumbbell Seated Triceps Extension | $0.033975 | no | "One figure on a plain background demonstrating the exercise \"Dumbbell Seated Triceps Extension\" in two positions. Start position: the figure sits on a bench with a straight back and feet flat on the ground, holding a dumbbell with both hands, arms extended straight up overhead. End position: the figure remains seated in the same posture, elbows bent, the dumbbell lowered behind the head with the upper arms close to the ears. No text, letters, numbers, labels or arrows in the picture." |
| Band Horizontal Pallof Press | $0.034027 | no | "One person on a plain background demonstrating a band horizontal Pallof press: they stand perpendicular to a resistance band anchored at waist height, feet shoulder-width apart, gripping the band handle with both hands. At the start of the movement the hands are drawn in at the chest with elbows bent and close to the torso, the band taut; at the far end of the movement the arms are straight and extended forward from the chest, pressing the band away while the torso stays upright and stable. No text, letters, numbers, labels or arrows in the picture." |

Every brief ends with an explicit "No text, letters, numbers, labels or arrows" instruction, and the full brief (start and end position as well as the prompt) is in each `.brief.json`. The pictures are 1408×768 JPEGs with both positions side by side; I looked at them downscaled to 512 px.

**Comparison with finding 8 (Gemini, same six).**

| Exercise | Finding 8 (#523, plain-name prompt) | Finding 9 (generated brief) |
|---|---|---|
| Pull-up | printed label "Hanging Body Body" | no text. Hands stay on the bar in both positions, but the second figure is only slightly raised, so the top position may not show a full pull-up (owner's call) |
| Bench press | printed labels, "Babows" and "Spotter's Arms" wrong | no readable text (the plates carry small marks, not words) |
| Face pull | rope ended across the face, elbows flared | no text. The rope ends with the hands beside the ears, not across the face (owner's call on form) |
| Lateral raise | labels not itemised in finding 8 | no text |
| Triceps extension | labels not itemised in finding 8 | no text |
| Pallof press | labels not itemised in finding 8 | no text; shown standing facing the wall anchor, not perpendicular as the brief says (owner's call) |
| Wrong labels | 3 named in finding 8 (the finding counted no total) | 0 |
| Pictures with printed text | at least 2 of 6 named (pull-up, bench press) | 0 of 6 |

Finding 8 did not record text for the other four exercises, so the before-count is a lower bound. The labels did not print at all this time, so the next lever the ticket asks for (a negative instruction, which the briefs already contain, or Recraft vector) is not needed on the evidence of these six pictures. This is one picture per exercise, so it shows no more than that the instruction held six times. Form accuracy is not judged here; the owner's verdict is on the samples page: https://claude.ai/artifact/U6PA6YBpfq8irZfRYt1Zkh. The pictures are not committed.

**Spend.** Command lines summed by hand: $0.033950 + $0.033993 + $0.033944 + $0.033981 + $0.033975 + $0.034027 = **$0.203870**, against the cap of $0.50. Each line's `spent_usd` is the command's own figure, which includes the brief call (about $0.0004) and the picture call. A seventh run, "Bench Press", was refused before any call (`result=skipped reason=no_such_exercise spent_usd=0`): the catalogue names it "Barbell Bench Press". Cost source is the command output only; `get-current-key` was not read in this run.

**The earlier blocked attempt.** The first try on 2026-11-11 stopped on "Pull-Up" because the command's image call timed out on a 5 s default and then its spend guard refused a retry (`cost_unknown`); finding 10 has the cause, and #543, #544 and #547 fixed the command. About $0.0004 was spent before that.

### 10. Why the picture call fails (#544)

Read on 2026-11-11. The picture command (`generate-exercise-picture`, #533) failed on 2026-11-11 in both runs, #534's and #544's: #534's image call ended `error` at 5003 ms, and #544's run stopped on its search call before any image call, while the MCP `generate-image` tool drew six pictures with the same model on 2026-11-08 (finding 8). The cause is in our wiring, not at OpenRouter or Google. The command's HTTP client has no timeout, so httpx2's 5 s default applies, and the image model takes about 5.4 s or longer. Paid calls were made in this ticket with the owner's yes and a cap of $0.10 (the command once, and two direct adapter probes, no athlete data). Their outputs were recorded in the ticket's state file; this finding reads them and runs nothing.

**The failing call.** The command's last line was `generate-exercise-picture: result=cap_reached reason=cost_unknown template=knowledge.query spent_usd=unknown price_usd=0.0336 cap_usd=0.1` (exit 1, `Pull-Up`, 2026-11-11 12:49 UTC, the `ai_trainer_wt544` database). So the run did not reach the image call: it stopped on the search call's row (see "The `cost_unknown` row"). The image call itself was probed twice through the same adapter (`OpenRouterImageGenerator`) with finding 8's Pull-Up prompt (2026-11-11):

| Client | Elapsed | Result | Row in `llm_calls` |
|---|---|---|---|
| `httpx2.AsyncClient()` as the command builds it | 5.04 s | `ImageGenerationFailed(reason='error', cause='transport', status=None, error_code='ReadTimeout', error_message=None)`; the command would print `result=failed step=image reason=error cause=transport error=ReadTimeout` | `outcome=error`, tokens 0/0, cost NULL, latency 5043 ms |
| `httpx2.AsyncClient(timeout=120.0)` | 5.37 s | `image/jpeg`, 253,797 bytes | `outcome=success`, cost $0.0336035, tokens 14/1120, latency 5369 ms |

**HTTP status: none. Error body: none.** The failing call never got a response: the client raised `httpx2.ReadTimeout` at 5 s, before OpenRouter answered, so there was no status line and no body to read. What the command reports in their place is the exception class, `cause=transport error=ReadTimeout` (the probe above, 2026-11-11). #534's image failure on the same day was recorded before #543, so only its row remains: `media.picture` on `google/gemini-3.1-flash-lite-image`, `outcome=error`, 5003 ms, cost NULL (`ai_trainer_wt534`, read-only SQL, 2026-11-11). That its cause was the same timeout is inferred from the 5003 ms and from the probe reproducing it, not recorded.

**Why 5 s.** The command builds its client as `httpx2.AsyncClient(transport=hooks.transport)` with no timeout (`src/ai_trainer/cli_media.py:175`). httpx2 2.16.0's default is `Timeout(timeout=5.0)` (`.venv/.../httpx2/_config.py:212`; the same default as [httpx's timeout docs](https://www.python-httpx.org/advanced/timeouts/), read 2026-11-11, which describe the read timeout as per chunk, not a total). The adapter's own timeout is `IMAGE_GENERATION_TIMEOUT_SECONDS`, 120 s (`src/ai_trainer/settings.py:139`), but `CallAccountant` applies it with `asyncio.wait_for` (`src/ai_trainer/llm/call_accounting.py:66-69`), so it never fires before the client's 5 s. `_post` turns the `httpx2.ReadTimeout` into `_failed("transport", error_code="ReadTimeout")` and returns `CallUsage()` (`src/ai_trainer/llm/images.py:211-224`). The web app builds its query client with `timeout=settings.knowledge_query_timeout_seconds` (`src/ai_trainer/main.py:508-510`) and the embedding gateway with `embedding_call_timeout_seconds` (`main.py:533-534`); only this command forgot to.

In the one 30-minute window read, the model's typical call was slower than 5 s on the upstream that served nearly all requests. `list-model-endpoints` (2026-11-11): Google Vertex p50 6,862 ms, p75 8,759, p90 10,935, p99 17,971 over 1,813 requests; Google AI Studio p50 5,394 ms over 21 requests. Our one success took 5.37 s. `get-endpoint-uptime-history` (2026-11-11, 10:00 to 12:00 UTC): Vertex 100.0% each hour, AI Studio too little traffic. The failures at 11:59 and 12:49 UTC are not explained by an outage in that data. The OpenRouter and Google status pages are rendered by script and returned no incident text to a fetch (`status.openrouter.ai` and `aistudio.google.com/status`, 2026-11-11), so they were not checked in a way that could rule an incident out.

**Why the brief survived 13.4 s on that client.** The brief goes through `OpenRouterGateway` and Pydantic AI, which passes our client to the OpenAI SDK (`src/ai_trainer/llm/gateway.py:104-105`). The SDK ignores a client whose timeout equals httpx's default and sends its own timeout on each request: `DEFAULT_TIMEOUT = Timeout(timeout=600, connect=5.0)` (`openai/_constants.py:7`; the check is in `openai/_base_client.py:1017-1029`, and the request uses it at `:682`). So the brief had a 600 s read timeout. The embedding adapter and the image adapter call `client.post` directly with no timeout argument (`src/ai_trainer/llm/images.py:213-217`, `src/ai_trainer/llm/embeddings.py:167`), so they get the client's 5 s. The brief took 13,407 ms and succeeded (`media.picture_brief`, `z-ai/glm-5.3-flash`, cost $0.000383), which fits. This was read from the installed library source, not run.

**Request comparison.** Our request and the MCP tool's, as far as the docs and schema show (`search-docs` and the [image-generation guide](https://openrouter.ai/docs/guides/overview/multimodal/image-generation), 2026-11-11; `generate-image` schema through ToolSearch, not called):

| | Our adapter | MCP `generate-image` | OpenRouter docs |
|---|---|---|---|
| Route | `POST https://openrouter.ai/api/v1/images` (`images.py`) | Not visible in the schema | `POST /api/v1/images`; `get-model` lists `/api/v1/images` for this model |
| Body | `{"model", "prompt"}` | `model`, `prompt`, optional `aspect_ratio`, `resolution`, `reference_image_urls` (1:1 was set at #523) | `model` and `prompt` required; `n`, `resolution`, `aspect_ratio`, `size`, `quality`, `output_format`, `seed`, `stream`, `provider.*` and more optional |
| Headers | `Authorization: Bearer`, JSON body | Not visible | `Authorization: Bearer`, `Content-Type: application/json` |
| Client timeout | 5 s (httpx2 default) | **Not visible**: the tool's own client timeout cannot be read from its schema or the docs | None documented. The guide's example took 94 s |

The body is valid per the docs, and the same body drew a picture once the timeout was raised. The one difference that matters is the client timeout.

**The `cost_unknown` row.** The row is `knowledge.query`, `qwen/qwen3-embedding-8b`, `outcome=error`, tokens 0/0, cost NULL, 5065 ms, at 12:49:13 UTC in `ai_trainer_wt544`. It is the search for the exercise's description, which runs before the brief (`src/ai_trainer/main.py:391-402`). Its embedding call is made on the same 5 s client (`main.py:400-402` passes the command's `http_client` to `build_knowledge_search`). The three `knowledge.query` rows of 2026-11-11 took 4,970 ms (success), 5,049 ms and 5,065 ms (both `error`), so the call sits on the 5 s limit. That the two errors were client timeouts is inferred from those latencies; the embedding call was not probed on its own. Why the row has no cost, from the code: the embedding adapter does not catch `httpx2.HTTPError` (`src/ai_trainer/llm/embeddings.py:157-172`), so a `ReadTimeout` propagates to `CallAccountant.attempt`, whose `except Exception` records outcome `error` (`src/ai_trainer/llm/call_accounting.py:76-77`) with an empty `CallUsage()` (`:81-82`), and an empty `CallUsage` has `cost=None` (`:32`). The image adapter reaches the same NULL another way: it catches the error and returns `CallUsage()` itself (`images.py:211-224`). `RunSpend.record` sets `cost_unknown` and remembers the template on any row with no cost (`src/ai_trainer/application/exercise_picture.py:66-73`), and `_stop_before` runs before the brief (`:147-148`) and before the image (`:114`). So the run stops with `reason=cost_unknown template=knowledge.query` before any other call. #534's retry at 12:01 UTC stopped the same way: its `knowledge.query` row was `outcome=error`, cost NULL, 5049 ms. The `template=` field is what #543 added to the output, which is how this was seen.

**Billing of a timed-out call is unknown.** A call that timed out has no generation ID and no cost row. OpenRouter's guide says a generation that does not complete returns 502 with no charge, and that if a client disconnects the upstream may still finish and charge OpenRouter but the user is not billed ([image-generation guide](https://openrouter.ai/docs/guides/overview/multimodal/image-generation), 2026-11-11). We did not verify that for our key. The MCP's `get-current-key` is not the app's key: the $0.000383 brief at #534 never showed on it, so it cannot confirm the app's spend (2026-11-11). The app's own spend is only what its `llm_calls` rows say.

**Who owns the fix.** Our wiring. The failing calls never reached an OpenRouter or Google error: the client closed first, the route and body are valid, the model's p50 to p90 was about 5 to 11 s in the window `list-model-endpoints` reported (AI Studio's 5.4 s p50 from only 21 requests), and one run with a long timeout drew the picture. Nothing here needs OpenRouter or Google to change. Two things are not settled by this finding: whether the MCP's own client has a longer timeout (not visible), and what a timed-out call costs upstream.

**Next ticket.** #547, `backend: give the picture command's HTTP client configured timeouts` (area backend, size S). This finding named it; the owner had it created afterwards, on 2026-11-11, and #534 is now blocked by it. Build the client in `cli_media.py:175` with a timeout from `Settings` (at least `image_generation_timeout_seconds` for the image call and `knowledge_query_timeout_seconds` for the search), or pass the timeout per call in the adapters as the web app already does. Add a test that a call slower than 5 s on a fake transport is not cut off, and that a transport failure is shown. A second, separate question for that ticket: whether a failed search should stop the whole run on `cost_unknown` (ADR-0031 *Picture generation (#531)*, the "Cap" bullet, `docs/adr/0031-exercise-catalogue-and-name-resolution.md:142`), since a search that never reached the provider has no cost to fear. That would need `/update-docs` first, so it is only named here.

## Recommendations for this project

> Decision, 2026-11-07 (#514): the owner picked `wger` and rejected Gym visual on cost. See "Decision" below and ADR-0031. The inputs that follow are kept as written.

These are inputs for the owner and for the M2m ticket. None of them decides anything, and none contradicts ADR-0031: it said when this was written that no media is imported "until a source with a license we hold is chosen". The choice, made 2026-11-07, is in the Decision section below.

- Treat `free-exercise-db` images as unusable. Its text and JSON may still be useful as data, which is outside this ticket.
- Before choosing, read Gym visual's terms in a browser and ask them for a quote. It has the largest catalogue (1,324 exercises) and 3 clear hits plus 2 families on the first session, with no face pull. It is the dataset ADR-0031 already imports ([ADR-0031](../adr/0031-exercise-catalogue-and-name-resolution.md), read 2026-11-02), so its media needs no matching between datasets. Its media is 180×180 only ([NOTICE.md](https://github.com/hasaneyldrm/exercises-dataset/blob/main/NOTICE.md)), which the owner should check against the F23 card's media slot. If the price, the size or the app-use rule rules it out, the answer is already in hand.
- Consider `wger` as the free fallback only if the project can meet CC BY-SA attribution (author, licence link) per image, and only after deciding what the 104 images with no author, and the 46 AI-flagged ones, mean for G10's "shown with its source". The admin review step in F24 is where such a picture would be skipped.
- An SVG set involves no third-party image owner, and it can cover face pull. Whether such output can be owned at all is an Open question, and its limit is form accuracy, which a human reviewer must judge. The samples let the owner judge whether the style is acceptable at all.
- Run a small image-model sample only if the owner asks. It is the one paid step and the least known on rights.
- If the owner picks an option that changes ADR-0031's media tables or the PRD's F24 wording, that change goes through `/update-docs`.

## Decision (2026-11-07, #514)

The owner picked `wger` as the first source: `licensed`, `stored`, CC BY-SA per image. Gym visual is rejected (cost: a side project that will not pay for content; G16 *Out* until permission is held), and `free-exercise-db` is rejected as G16 *Out* (finding 1). This document still decides nothing: the pick, the tier, the delivery and the attribution text are in [ADR-0031, "First media source (#514)"](../adr/0031-exercise-catalogue-and-name-resolution.md).

## Open questions

Resolved at #514 (2026-11-07) for the `wger` pick:

- **Gym visual terms, price and app use.** Answered: ruled out by the owner on cost. The terms were read in a browser by the owner, who recorded no price. Not pursued further.
- **CC BY-SA and in-app use.** Answered as a rule, not as law: serve the file unaltered, scaled by CSS only, with credit and licence link beside it. The remaining adaptation risk is accepted by the owner (side project, G17), and a rights holder's removal request is honoured at once (G16).
- **`wger` images with no author.** Answered by the owner: skipped at import, since a `licensed` row needs an attribution.
- **AI-flagged `wger` images.** Answered by the owner: kept eligible, labelled "AI generated" beside the credit, and judged in admin review.
- **Whether the catalogue's `wger` matches are the right variants.** Answered: an admin looks at each picture, which F24 already requires.

Not needed for the pick (they concern options not chosen):

- **Other open image sets.** Not needed for the pick.
- **OpenAI's terms on output.** Not needed for the pick.
- **Copyrightability of AI output.** Not needed for the pick.
- **Which Anthropic terms govern a Claude-written SVG used in the product.** Not needed for the pick.
- **Cost of an SVG, and where it is stored.** Not needed for the pick.
- **Per-image cost of the image models.** Measured for two models at #523 (finding 8). Still open for `openai/gpt-image-2.6-sunburst`, which was not run.
- **Form accuracy of an image model and of Gym visual's pictures.** Samples exist for two image models (finding 8); on 2026-11-08 the owner picked `google/gemini-3.1-flash-lite-image` as the fallback source (ADR-0031, "Fallback media source (#524)"); no accuracy verdict is recorded here (a re-sample with generated briefs, finding 9 of #534, is awaiting the owner's verdict). Gym visual's pictures remain unjudged.

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
| [CJEU C-466/12 *Svensson*](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:62012CJ0466) | Court ruling | judgment 2014-02-13, read 2026-11-07 |
| [CJEU C-348/13 *BestWater*](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:62013CO0348) | Court ruling (French text) | order 2014-10-21, read 2026-11-07 |
| [CJEU C-160/15 *GS Media*](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:62015CJ0160) | Court ruling | judgment 2016-09-08, read 2026-11-07 |
| [CJEU C-392/19 *VG Bild-Kunst*](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:62019CJ0392) | Court ruling | judgment 2021-03-09, read 2026-11-07 |
| [First gym session transcript](../../evals/field/gym/2026-10-28-first-gym-session.md) | Project data | 2026-10-28 |
| [Recraft Terms of Service](https://www.recraft.ai/terms) | Provider's own terms | effective 2026-10-21, read 2026-11-08 |
| [Gemini API additional terms](https://aistudio.google.com/docs/terms) | Provider's own terms | effective 2026-03-23, read 2026-11-08 |
| OpenRouter `generate-image` and `get-current-key` (12 sample pictures, $0.681621) | OpenRouter MCP, paid, owner-approved with a $1 cap | 2026-11-08 |
| [Generated exercise pictures](https://claude.ai/artifact/BgoG9dnEuAkhsxVU9sJwut) | Private Artifact page, this ticket | 2026-11-08 |
| OpenRouter `get-model`, `list-model-endpoints`, `get-endpoint-uptime-history` (`google/gemini-3.1-flash-lite-image`), `search-docs`, and the `generate-image` schema (not called) | OpenRouter MCP, read-only | 2026-11-11 |
| [OpenRouter image generation guide](https://openrouter.ai/docs/guides/overview/multimodal/image-generation) | Official documentation | 2026-11-11 |
| [httpx timeouts](https://www.python-httpx.org/advanced/timeouts/) | Official documentation | 2026-11-11 |
| OpenRouter status page and Google AI Studio status page | Status pages (script-rendered; no incident text returned) | 2026-11-11 |
| httpx2 2.16.0 and openai SDK sources in `.venv` | Installed library source | 2026-11-11 |
| `llm_calls` rows and two adapter probes for #544 (`ai_trainer_wt544`, `ai_trainer_wt534`) | Project data, owner-approved $0.10 cap | 2026-11-11 |
