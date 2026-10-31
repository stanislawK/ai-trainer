# Research — Hybrid search on PostgreSQL

| | |
|---|---|
| Ticket | #371 |
| Read on | 2026-10-31 |
| Feeds | ADR-0011, ADR-0033, ADR-0031; the embedding-model spike, the `search_knowledge` ticket and the M3 exercise-shortlist work |

**Versions this document targets.** PostgreSQL 18.6 and pgvector 0.8.6 in the dev image `pgvector/pgvector:pg18-trixie`; pgvector 0.8.7 in the digest-pinned production image (`compose.prod.yaml`). Both were read from the running containers (`select version()`, `pg_available_extensions`) on 2026-10-31. The two pgvector patch versions differ because the dev tag floats and the production digest does not. pgvector's own README at read time documents 0.8.7 ([README](https://github.com/pgvector/pgvector/blob/master/README.md), read 2026-10-31).

## Questions

1. How should a vector ranking and a full-text ranking be fused, and what are the alternatives?
2. Which HNSW settings and column type (`vector` or `halfvec`) fit this project?
3. How is a search filtered by sport and topic when the vector index is approximate?
4. How is English and Polish full-text search set up on PostgreSQL 18?
5. How can an English question find a Polish passage?
6. Which cheap checks tell us retrieval works, without a large dataset?
7. How could the same search serve the exercise shortlist (ADR-0031)?
8. What must the embedding-model spike check? (The model is not chosen here.)

## Findings

### 1. Fusing vector and full-text rankings

- pgvector's README names two ways to combine results from vector search and Postgres full-text search: Reciprocal Rank Fusion and a cross-encoder ([pgvector README, Hybrid Search](https://github.com/pgvector/pgvector/blob/master/README.md), v0.8.7, read 2026-10-31).
- RRF scores a document as the sum over rankings of `1 / (k + rank)`. The paper fixed `k = 60` in a pilot and did not change it afterwards. Its pilot table shows MAP of .2072 at `k = 0` rising to .2147 at `k = 80` and falling to .2098 at `k = 500`, and the authors call the choice "not critical" ([Cormack, Clarke, Büttcher, SIGIR '09](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf), read 2026-10-31).
- The same paper reports RRF beating Condorcet Fuse in all four fusion experiments on TREC collections, beating CombMNZ in three of four, and beating the best individual system in three of four. The exception was TREC 9, where the best system used a human in the loop. It reports a 4% to 5% average gain over Condorcet, CombMNZ and the best system ([same paper](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf), Tables 1 and 2). These are TREC web and ad hoc runs, not a mixed-language knowledge base.
- The formula sums over any number of rankings and uses ranks only, so it needs no score normalisation between systems ([same paper](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf)).
- pgvector's reference implementation runs the vector query and the `ts_rank_cd` query as two CTEs with `RANK() OVER (...)`, each `LIMIT 20`. It joins them with a `FULL OUTER JOIN` and scores `COALESCE(1.0 / (k + rank), 0.0)` per side, with `k = 60` ([`rrf.py`](https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/rrf.py), master, read 2026-10-31).
- Alternative, weighted score sum: MIRACL's hybrid is `α · s_BM25 + (1 − α) · s_mDPR` with both scores normalised to [0, 1] and `α = 0.5` untuned. Across its 18 languages the dev-set average nDCG@10 was 0.393 for BM25, 0.415 for mDPR and 0.578 for the hybrid. Indonesian was the exception, where the hybrid scored 0.443 against BM25's 0.449 ([MIRACL, arXiv 2210.09984 v1](https://arxiv.org/abs/2210.09984), Section 4 and Table 2, read 2026-10-31). Polish is not one of its languages.
- Alternative, cross-encoder re-ranking: pgvector's example takes the top 5 of each search, de-duplicates, and re-scores the (query, text) pairs with a cross-encoder ([`cross_encoder.py`](https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/cross_encoder.py), master, read 2026-10-31). The example needs a model run per candidate. Its cost and language coverage here are not settled by the source.
- Full-text ranks come from `ts_rank` or `ts_rank_cd`. The manual says ranking "can be expensive since it requires consulting the tsvector of each matching document". `ts_rank_cd` also weighs how close the matching words are. Its normalisation flags divide by document length, unique words, or `rank + 1` ([PostgreSQL 18, Controlling Text Search](https://www.postgresql.org/docs/18/textsearch-controls.html), read 2026-10-31).
- BEIR, a zero-shot English benchmark, finds BM25 "a robust baseline" and dense retrievers computationally efficient "but often underperform". It does not evaluate fusion ([BEIR, NeurIPS 2021 Datasets & Benchmarks, arXiv 2104.08663 v4](https://arxiv.org/abs/2104.08663), read 2026-10-31).

### 2. HNSW settings, `vector` or `halfvec`

- Defaults: `m = 16`, `ef_construction = 64`, `hnsw.ef_search = 40`. `ef_construction` must be at least `2 * m` ([pgvector README, HNSW](https://github.com/pgvector/pgvector/blob/master/README.md), v0.8.7, read 2026-10-31; [pgvector `hnsw.h` and docs via context7](https://github.com/pgvector/pgvector/blob/master/src/hnsw.h), read 2026-10-31).
- Build time is much shorter when the graph fits in `maintenance_work_mem`. Parallel build uses `max_parallel_maintenance_workers` (2 by default) ([README, index build time](https://github.com/pgvector/pgvector/blob/master/README.md), v0.8.7). PostgreSQL 18 also allows GIN indexes to be created in parallel ([PostgreSQL 18 release notes, E.6.3.1.2](https://www.postgresql.org/docs/18/release-18.html), read 2026-10-31).
- An HNSW index covers `vector` up to 2,000 dimensions and `halfvec` up to 4,000. A `halfvec` column can hold up to 16,000 ([README, vector and halfvec types](https://github.com/pgvector/pgvector/blob/master/README.md), v0.8.7).
- Storage per value is `4 * dimensions + 8` bytes for `vector` and `2 * dimensions + 8` for `halfvec` ([same README](https://github.com/pgvector/pgvector/blob/master/README.md)). A 1,024-dimension value is 4,104 bytes as `vector` and 2,056 as `halfvec`.
- An index on a cast expression is documented: `CREATE INDEX ON items USING hnsw ((embedding::halfvec(3)) halfvec_l2_ops);` ([same README](https://github.com/pgvector/pgvector/blob/master/README.md)). The source shows the form with a fixed dimension.
- Relevant fixes in the 0.8 line: 0.8.0 added iterative scans and better cost estimation when filtering. 0.8.2 fixed "Index Searches in EXPLAIN output for Postgres 18". 0.8.3 fixed possible index corruption with HNSW vacuuming. 0.8.4 fixed errors with HNSW vacuuming and inserts ([pgvector CHANGELOG](https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md), read 2026-10-31). Both installed versions (0.8.6, 0.8.7) include all of these.
- An unreleased 0.9.0 section lists asynchronous I/O for HNSW ([same CHANGELOG](https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md)). It is not in either installed version.
- By default pgvector does exact nearest-neighbour search with perfect recall. An index "trades some recall for speed" ([README, exact search](https://github.com/pgvector/pgvector/blob/master/README.md), v0.8.7, via context7 on 2026-10-31).
- The sources read give no corpus size at which HNSW starts to pay off.

### 3. Filtering by sport and topic with an approximate index

- "With approximate indexes, filtering is applied after the index is scanned." With `hnsw.ef_search = 40`, a condition that matches 10% of rows leaves about 4 results ([README, Filtering](https://github.com/pgvector/pgvector/blob/master/README.md), v0.8.7, read 2026-10-31).
- Options the README gives:
  - a partial index per filter value, `CREATE INDEX ... USING hnsw (...) WHERE (category_id = 123)`, for a few distinct values;
  - partitioning (`PARTITION BY LIST`), for many distinct values;
  - iterative index scans, from 0.8.0, which keep scanning until enough rows pass the filter or a limit is reached. Modes are `strict_order` and `relaxed_order`. `hnsw.max_scan_tuples` is 20,000 by default, and `hnsw.scan_mem_multiplier` is 1 by default ([same section](https://github.com/pgvector/pgvector/blob/master/README.md#iterative-index-scans)).
- The README text read gives no default for `hnsw.iterative_scan` itself. The README's own examples enable it with `SET`.
- The project's filters are `sports` and `topics` (ADR-0011), `exercise_id` (ADR-0033), and the embedding model (ADR-0011 invariant 4: chunks from different models are never searched together). The first three are columns on a chunk and the model is a column on every chunk. The sources do not say how many distinct values a partial index stops being practical at, beyond "a few".

### 4. English and Polish full-text on PostgreSQL 18

- The `pg_ts_config` of the project's PostgreSQL 18.6 image lists these configurations: arabic, armenian, basque, catalan, danish, dutch, english, estonian, finnish, french, german, greek, hindi, hungarian, indonesian, irish, italian, lithuanian, nepali, norwegian, portuguese, romanian, russian, serbian, simple, spanish, swedish, tamil, turkish, yiddish. Polish is not among them (queried 2026-10-31). The manual's own example listing of Snowball dictionaries agrees ([PostgreSQL 18, psql text-search commands](https://www.postgresql.org/docs/18/textsearch-psql.html), read 2026-10-31). PostgreSQL 18 added Estonian stemming, and the list above has no Polish ([release notes, E.6.3.3](https://www.postgresql.org/docs/18/release-18.html)).
- The `simple` dictionary lowercases the token and checks it against stop words. It does no stemming ([PostgreSQL 18, Dictionaries](https://www.postgresql.org/docs/18/textsearch-dictionaries.html), read 2026-10-31).
- A dictionary-based route exists: "The standard PostgreSQL distribution does not include any Ispell configuration files", but Hunspell and MySpell dictionaries can be installed by copying `.affix` and `.dict` files, converted to UTF-8, into `$SHAREDIR/tsearch_data` and declared with `CREATE TEXT SEARCH DICTIONARY ... (TEMPLATE = ispell, ...)` ([same page](https://www.postgresql.org/docs/18/textsearch-dictionaries.html)). Installing files into `$SHAREDIR` means changing the Postgres image. The page does not name a Polish dictionary, its licence, or its quality.
- The `unaccent` module is described on the same page as a way to strip accents. This project already uses `unaccent` for aliases (ADR-0031).
- GIN is "the preferred text search index type" ([PostgreSQL 18, Text Search Indexes](https://www.postgresql.org/docs/18/textsearch-indexes.html), read 2026-10-31).
- A stored generated column keeps the `tsvector`. In PostgreSQL 18 generated columns are virtual by default, so the manual's pattern needs `STORED` written out ([Tables and Indexes](https://www.postgresql.org/docs/18/textsearch-tables.html); [release notes, E.6.3.2](https://www.postgresql.org/docs/18/release-18.html), both read 2026-10-31).
- The manual also shows a per-row configuration: an expression index `to_tsvector(config_name, body)` with queries that repeat the same expression ([Tables and Indexes](https://www.postgresql.org/docs/18/textsearch-tables.html)). It does not show a stored column that chooses the configuration from another column.
- PostgreSQL 18 changed the collation provider used by full-text search, and the release notes recommend reindexing full-text and `pg_trgm` indexes after an upgrade ([release notes, E.6.2](https://www.postgresql.org/docs/18/release-18.html)).
- ADR-0033 already fixes `english` for English chunks and `simple` for Polish. The findings above agree with that.

### 5. Finding Polish passages from English questions

- The vector leg is the only leg expected to cross languages, and only if the embedding model places both languages in one space, which ADR-0033 requires of the chosen model. That a full-text leg cannot do it is an inference, listed under Open questions, not a cited result.
- No source read measures English-to-Polish retrieval. MIRACL covers 18 languages, not Polish ([MIRACL](https://arxiv.org/abs/2210.09984), read 2026-10-31).
- PIRB, a Polish benchmark of 41 retrieval tasks, evaluates over 20 dense and sparse models and reports that hybrid methods improve results further ([PIRB, arXiv 2402.13350 v2](https://arxiv.org/abs/2402.13350), abstract, read 2026-10-31). Its body, including any BM25-versus-dense numbers and any cross-language tasks, was not read.
- PL-MTEB has 30 Polish tasks in five categories, including information retrieval, of which 12 are new additions to MTEB, and evaluates 30 embedding models ([PL-MTEB, arXiv 2405.10138 v2](https://arxiv.org/abs/2405.10138), abstract; the page notes acceptance to ACL 2026 Findings; read 2026-10-31). Whether MTEB itself covers Polish was not checked ([MTEB, arXiv 2210.07316 v3](https://arxiv.org/abs/2210.07316), abstract).
- OpenRouter's embeddings request accepts `dimensions`, `encoding_format` (`float` or `base64`) and `input_type` (e.g. `search_query`, `search_document`), and takes an array of strings as input. With `require_parameters` off, a provider "will receive only the parameters they support, and ignore the rest", so an unsupported `dimensions` can be dropped silently ([OpenRouter embeddings API reference](https://openrouter.ai/docs/api/api-reference/embeddings/submit-an-embedding-request.md), no version shown, read 2026-10-31). The page does not say which models honour `dimensions` or `input_type`.

### 6. Cheap retrieval checks

- Recall of the approximate index: run the same query once with the index and once with `BEGIN; SET LOCAL enable_indexscan = off; ...; COMMIT;`, which forces exact search, and compare ([pgvector README, Monitoring](https://github.com/pgvector/pgvector/blob/master/README.md), v0.8.7, via context7 on 2026-10-31).
- Plan and cost: `EXPLAIN (ANALYZE, BUFFERS)`. `ANALYZE` runs the query and implies `BUFFERS`. Buffer counts are cumulative over child nodes, and the measurement overhead "can be significant" ([PostgreSQL 18, Using EXPLAIN](https://www.postgresql.org/docs/18/using-explain.html), 18.6 docs, read 2026-10-31; [README, Querying](https://github.com/pgvector/pgvector/blob/master/README.md)). Version 0.8.2 fixed the "Index Searches" line for PostgreSQL 18 ([CHANGELOG](https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md)), which both installed versions include.
- Ranking quality metrics: BEIR reports nDCG@10 and defines Recall@k as the fraction of relevant documents retrieved. It names MRR and MAP but argues binary rank metrics "fail to evaluate tasks with graded relevance judgements". MIRACL uses nDCG@10 and Recall@100 ([BEIR](https://arxiv.org/abs/2104.08663), Section 3.3 and Appendix G; [MIRACL](https://arxiv.org/abs/2210.09984), Sections 2 and 4; read 2026-10-31).
- None of the sources says how many labelled queries are enough. This document does not invent a number.

### 7. Serving the exercise shortlist (ADR-0031)

- ADR-0033 ingests each exercise's text as one chunk per exercise and language, tagged with `exercise_id`, `language` and `sports = {gym}`. ADR-0031 already searches names and aliases with trigram and `simple` full-text. Both ADRs defer the better shortlist to M3 and say it is measured then.
- RRF takes any number of rankings ([paper](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf)), so the trigram list of ADR-0031 can be a third list next to the vector and full-text lists. The reference code fuses two lists ([`rrf.py`](https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/rrf.py)), so a third needs one more CTE and one more term.
- The existing hard case is recorded in ADR-0031: "Rising straight arms on both sides" ranks 178th among dumbbell exercises on a Python approximation of trigram similarity. It is a ready first test for the vector leg.
- The vector leg adds a model call per search to embed the query. Whether that fits the logging response-time bound (PRD 0010 G14) is not settled by any source read.

## Recommendations for this project

These inform the later tickets and decide nothing.

- **Fuse with RRF at `k = 60`**, as in the paper and pgvector's `rrf.py` (finding 1). Treat the per-list depth as a spike parameter, since `rrf.py` uses 20 and gives no reason. A score sum would need score normalisation first, and MIRACL's gain there is on other languages (finding 1). This agrees with ADR-0033.
- **Do not add a cross-encoder yet.** It is a second model run per candidate and nothing here shows it is needed (finding 1).
- **Store the embedding as `vector(n)` when the chosen model's dimension is 2,000 or less, and as `halfvec(n)` above that, up to 4,000** (finding 2; ADR-0033 already cites these limits). Use `halfvec` for storage only if the recall check in finding 6 shows no loss. Always index with cosine operators (ADR-0011).
- **Keep HNSW defaults** (`m = 16`, `ef_construction = 64`, `ef_search = 40`) until the recall check against an exact scan says otherwise (finding 2, finding 6). ADR-0011 already specifies an HNSW index. Whether the first corpus is large enough to need it is a measurement, not a given (finding 2).
- **Filter with iterative scans (`relaxed_order` or `strict_order`) as the default**, and raise `hnsw.max_scan_tuples` only if the recall check fails (finding 3). The sports are few, so a partial index per sport is possible later. Topics are many, so they are not a candidate for partial indexes.
- **Do not filter on `language` in the vector leg of a user question.** That filter would remove the Polish passages an English question should find (finding 5). Filter the full-text leg by language instead, because its configuration differs by language (finding 4).
- **Embedding model filter:** because chunks from different models are never searched together (ADR-0011 invariant 4), every search filters on the model. With one model in use, the filter removes nothing. When a second model arrives, the spike must decide between a partial index per model and a re-embed (finding 3).
- **Full-text:** `english` for English chunks and `simple` for Polish, in a stored generated column with a GIN index, and `STORED` written out (finding 4). Run the full-text leg per language configuration. Polish stemming through a Hunspell dictionary is an option to evaluate, not a recommendation (Open questions).
- **Check retrieval before choosing parameters** with the cheap checks in finding 6: a small set of labelled questions (the first one being the ADR-0031 case), nDCG@10 and Recall@100, an exact-versus-index recall comparison, and `EXPLAIN (ANALYZE, BUFFERS)`.
- **For the exercise shortlist, reuse `search_knowledge`** with `sports = {gym}` and group the hits by `exercise_id`, adding the trigram list as a third RRF input (finding 7). Whether to call it on the logging hot path is for M3 to decide after measuring latency (G14).

### What the embedding spike must check

The model is chosen by the spike (ADR-0033), not here. The spike checks each candidate on:

1. English and Polish text in one space (ADR-0033), shown on this project's own English-to-Polish pairs, because no source read measures it (finding 5).
2. Output dimension: at most 2,000 for `vector`, 4,000 for `halfvec` (finding 2). Check whether the model honours `dimensions` through OpenRouter, with `require_parameters` set so that an ignored parameter fails loudly (finding 5).
3. Whether it takes `input_type` (query versus document) and whether that helps on this corpus (finding 5).
4. Maximum input length against the chunk size chosen at the ingestion ticket (ADR-0033).
5. Price per token and per catalogue import, from the OpenRouter MCP `list-models` and `get-model`, with the date (`.claude/rules/openrouter-mcp.md`).
6. A stable, generally available model ID (CLAUDE.md).
7. Published Polish retrieval results, for example PIRB and PL-MTEB ([PIRB](https://arxiv.org/abs/2402.13350), [PL-MTEB](https://arxiv.org/abs/2405.10138)), used only to screen candidates. The choice is made on this project's own checks (finding 6).
8. How the embedding call is accounted: one `llm_calls` row per call (ADR-0018, ADR-0033).

## Open questions

- Is a Polish Hunspell dictionary available under a licence we can ship, and does installing it into the Postgres image pass ADR-0023's image rules? The PostgreSQL manual explains how to install one but names none. Settled by a spike ticket that checks the dictionary's source and licence.
- Does a stored generated `tsvector` column work with a configuration chosen from the row's `language` column? The manual shows each of the two pieces (generated column; per-row configuration in an expression index) but not them together. Settled by trying it on the project's image in the ingestion ticket.
- Which tsquery configuration should a mixed question use? The expectation that a full-text leg cannot match an English query to a Polish passage is an inference from how lexeme matching works, not something a source read states. The vector leg is expected to cross languages (finding 5). Settled by the retrieval checks.
- What per-list depth and what RRF `k` suit this corpus? The paper says `k = 60` is "not critical" and `rrf.py` uses depth 20 without explanation. Settled by the retrieval checks.
- Does `halfvec` lose recall at the chosen dimension? No source read measures it. Settled by the exact-versus-index comparison.
- At what corpus size does HNSW beat exact search here? The sources give no threshold. Settled by `EXPLAIN (ANALYZE, BUFFERS)` on the real corpus.
- How well do multilingual embeddings retrieve Polish passages from English questions? Neither MIRACL (no Polish) nor the PIRB and PL-MTEB abstracts settle it, and the PIRB and PL-MTEB bodies were not read. Settled by the embedding spike on this project's own pairs.
- Does query embedding fit the logging response-time bound (PRD 0010 G14)? Settled by measuring in M3.
- Is pgvector 0.8.6 versus 0.8.7 between dev and production a problem? Nothing read suggests the retrieval behaviour differs (the 0.8.6 and 0.8.7 entries concern IVFFlat, sparsevec and `avg`), but pinning both to one tag is for the infra owner to decide.

## Sources

| Source | Kind | Version or date read |
|---|---|---|
| [pgvector README](https://github.com/pgvector/pgvector/blob/master/README.md) | Upstream docs | v0.8.7, 2026-10-31 |
| [pgvector CHANGELOG](https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md) | Upstream release notes | through 0.8.7 (2026-10-01), 2026-10-31 |
| [pgvector `hnsw.h`](https://github.com/pgvector/pgvector/blob/master/src/hnsw.h) | Upstream code (read through context7, `/pgvector/pgvector`) | master, 2026-10-31 |
| [pgvector-python `rrf.py`](https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/rrf.py) | Upstream code | master, 2026-10-31 |
| [pgvector-python `cross_encoder.py`](https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/cross_encoder.py) | Upstream code | master, 2026-10-31 |
| [Cormack, Clarke, Büttcher, Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods, SIGIR '09](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf) | Peer-reviewed | SIGIR 2009, read 2026-10-31 |
| [BEIR, Thakur et al.](https://arxiv.org/abs/2104.08663) | Peer-reviewed (NeurIPS 2021 Datasets & Benchmarks) | arXiv v4, 2026-10-31 |
| [MIRACL](https://arxiv.org/abs/2210.09984) | Paper (arXiv preprint; no venue shown on the page read) | arXiv v1, 2026-10-31 |
| [PIRB, Polish Information Retrieval Benchmark](https://arxiv.org/abs/2402.13350) | Paper (arXiv), abstract only | arXiv v2, 2026-10-31 |
| [MTEB](https://arxiv.org/abs/2210.07316) | Paper (arXiv), abstract only | arXiv v3, 2026-10-31 |
| [PL-MTEB](https://arxiv.org/abs/2405.10138) | Paper (arXiv; ACL 2026 Findings per the page), abstract only | arXiv v2, 2026-10-31 |
| [PostgreSQL 18: Controlling Text Search](https://www.postgresql.org/docs/18/textsearch-controls.html) | Official docs | 18.x, 2026-10-31 |
| [PostgreSQL 18: Dictionaries](https://www.postgresql.org/docs/18/textsearch-dictionaries.html) | Official docs | 18.x, 2026-10-31 |
| [PostgreSQL 18: Text Search Indexes](https://www.postgresql.org/docs/18/textsearch-indexes.html) | Official docs | 18.x, 2026-10-31 |
| [PostgreSQL 18: Tables and Indexes](https://www.postgresql.org/docs/18/textsearch-tables.html) | Official docs | 18.x, 2026-10-31 |
| [PostgreSQL 18: psql text-search commands](https://www.postgresql.org/docs/18/textsearch-psql.html) | Official docs | 18.x, 2026-10-31 |
| [PostgreSQL 18: Using EXPLAIN](https://www.postgresql.org/docs/18/using-explain.html) | Official docs | 18.6, 2026-10-31 |
| [PostgreSQL 18 release notes](https://www.postgresql.org/docs/18/release-18.html) | Official release notes | 18.x, 2026-10-31 |
| [OpenRouter embeddings API reference](https://openrouter.ai/docs/api/api-reference/embeddings/submit-an-embedding-request.md) | Official docs | no version shown, 2026-10-31 |
| Project's dev and production-smoke database containers (`select version()`, `pg_available_extensions`, `pg_ts_config`) | Installed software | PostgreSQL 18.6; pgvector 0.8.6 (dev) and 0.8.7 (prod image), 2026-10-31 |
