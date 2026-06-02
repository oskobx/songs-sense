# Phase 3a Spec: Hybrid Retrieval (BM25 + Semantic, RRF Fusion)

Adds a lexical (BM25) retrieval path alongside the existing semantic retrieval, with Reciprocal Rank Fusion to blend results. Modes use different combinations: Vibe Search uses semantic only, Find the Song uses hybrid, Lyric Twin uses semantic only with future diversity reranking.

## Goal

After 3a, the system supports three retrieval profiles tuned per mode, with hybrid available for queries that benefit from lexical exact-matching (lyric snippets, named entities).

## Design

### BM25 backend: Postgres full-text search

- Use Postgres's built-in `tsvector` + `ts_rank_cd`
- Text search config: `'english'` (default) — applies stemming, removes stopwords. Trades off literal matching for more flexible recall. Reasonable for lyrics where users typically remember words approximately.
- Indexed via GIN index on the `tsvector` column

### Fusion method: Reciprocal Rank Fusion (RRF)

For each candidate passage `p`:

```
rrf_score(p) = sum over rankers r:  1 / (k + rank_r(p))
```

where:
- `k = 60` (standard RRF constant; absorbs rank-noise)
- `rank_r(p)` is the 1-indexed position of `p` in ranker `r`'s top-N
- if `p` is not in ranker `r`'s top-N, its contribution from that ranker is 0

Two rankers: semantic (existing) and BM25 (new). Each contributes a top-100 list. RRF combines.

RRF is preferred over linear-blend because it sidesteps the score-normalization problem (semantic scores are 0-1 cosine, BM25 scores are unbounded ts_rank values).

### Per-mode retrieval profiles

| Mode | Retrieval | Notes |
|---|---|---|
| `vibe` | semantic only | Pure embedding match; language boost still applies; default mode |
| `find` | hybrid (semantic + BM25 via RRF) | Lyric-snippet recall |
| `twin` | semantic only | Style similarity; MMR diversity will be added in Phase 5 |

Mode selection via CLI flag: `--mode {vibe,find,twin}`. Default: `vibe`.

## Schema changes

```sql
-- Add tsvector column
ALTER TABLE passages ADD COLUMN passage_tsv tsvector;

-- Backfill from passage_text using english config
UPDATE passages SET passage_tsv = to_tsvector('english', passage_text);

-- GIN index for fast full-text search
CREATE INDEX passages_passage_tsv_idx ON passages USING GIN (passage_tsv);

-- Keep the tsvector in sync on future inserts/updates via trigger
CREATE TRIGGER passages_tsv_trigger
BEFORE INSERT OR UPDATE OF passage_text ON passages
FOR EACH ROW EXECUTE FUNCTION
tsvector_update_trigger(passage_tsv, 'pg_catalog.english', passage_text);
```

Index build time: ~1-2 min for 86k rows.

## Implementation

### Files

- `src/db/add_fts_column.py` — one-time migration script (adds column, backfills, creates index, creates trigger)
- `src/retrieval/bm25.py` — BM25 search function
- `src/retrieval/semantic.py` — extracts existing semantic search code into a function (refactor from `scripts/search.py`)
- `src/retrieval/hybrid.py` — RRF combiner taking lists of (passage_id, rank) tuples
- `scripts/search.py` — updated to support `--mode` flag, internally dispatches to the right retrieval profile

### Key functions

```python
# src/retrieval/semantic.py
def semantic_search(
    conn, query: str, query_lang: str, top_k: int = 100
) -> list[tuple[int, float]]:
    """Return [(passage_id, similarity), ...] sorted by similarity desc."""

# src/retrieval/bm25.py
def bm25_search(
    conn, query: str, top_k: int = 100
) -> list[tuple[int, float]]:
    """Return [(passage_id, ts_rank), ...] sorted by ts_rank desc."""

# src/retrieval/hybrid.py
def rrf_combine(
    rankings: list[list[tuple[int, float]]],
    k: int = 60,
    top_n: int = 10,
) -> list[tuple[int, float]]:
    """Combine multiple ranked lists via RRF. Returns [(passage_id, rrf_score), ...]."""
```

### BM25 SQL example

```sql
SELECT id,
       ts_rank_cd(passage_tsv, plainto_tsquery('english', %s)) AS rank
FROM passages
WHERE passage_tsv @@ plainto_tsquery('english', %s)
ORDER BY rank DESC
LIMIT 100
```

Uses `plainto_tsquery` (handles natural-language input cleanly).
Uses `ts_rank_cd` (cover-density rank, better for short text like passages).

### Final assembly query

After hybrid retrieval returns top-N passage IDs, fetch the full rows:

```sql
SELECT s.artist, s.title, s.year, s.tier, p.language, p.passage_text, p.id
FROM passages p
JOIN songs s ON p.song_id = s.id
WHERE p.id = ANY(%s)
```

Then map back to the RRF scores client-side, sort, display.

## Success criteria

- `passages.passage_tsv` populated for all 86,152 rows
- GIN index `passages_passage_tsv_idx` exists and is used (verify with EXPLAIN)
- BM25 search on `"shorty had them apple bottom jeans"` returns Flo Rida — Low in top 5
- Hybrid search on `"shorty had them apple bottom jeans"` returns Flo Rida — Low in top 3 (better than either alone)
- Vibe mode unchanged from previous behavior (regression check)
- Find mode produces different results than Vibe mode for the same query

## Sanity tests

After implementation, run these via `scripts/search.py`:

```bash
# Vibe mode (semantic only) - unchanged behavior
python scripts/search.py --mode vibe "feeling lost at night"

# Find mode (hybrid) - lyric snippet
python scripts/search.py --mode find "shorty had them apple bottom jeans"

# Find mode (hybrid) - mixed lyric + descriptive
python scripts/search.py --mode find "the song that goes da da da about California"

# Twin mode (semantic only)
python scripts/search.py --mode twin "I miss the days when we were young"
```

For each, print:
- Which mode/retrieval profile used
- Top 5 results with: artist, title, language, BM25 score (if applicable), semantic score (if applicable), RRF score (if applicable)
- Highlight the dominant signal contributing to each result's rank

## Implementation order

1. Add tsvector column + index + trigger (migration script)
2. Refactor existing search into `src/retrieval/semantic.py` function
3. Implement `src/retrieval/bm25.py`
4. Implement `src/retrieval/hybrid.py` with RRF
5. Update `scripts/search.py` to support `--mode` flag
6. Run sanity tests
7. Verify Vibe mode unchanged (regression check)

## Known risks

- **Stopword removal cuts important words**: "the song that goes" → stripped to nothing. Fine for queries with substantive content, fails for queries that are mostly fillers. We accept this; users with mostly-filler queries are out-of-distribution.
- **Stemming over-aggressive on lyrics**: "running" and "runs" both become `run`. Usually a feature, occasionally a bug. Acceptable.
- **Multilingual BM25 is English-config**: Polish/German query content gets weird stemming. For Find the Song specifically, this matters. We accept it for now and could add per-language configs later if eval shows it's bad.
- **RRF doesn't tune well**: it's parameter-free (well, k=60 is the only param), which is both a feature and a limitation. If we want to weight semantic more than BM25 explicitly, RRF doesn't support that. We can switch to weighted RRF or linear blending later if needed.
- **GIN index size**: tsvector index for 86k passages ~50-150 MB. Acceptable.

## Why this design

- Hybrid retrieval is the standard for production search systems (Google, Elasticsearch, every major search vendor)
- RRF is the simplest robust fusion method — used at Microsoft, OpenAI, many others
- Postgres FTS keeps everything in one database, no extra infrastructure
- Per-mode design makes the eval results interpretable: each mode's metrics measure what that mode is supposed to do
- Future-proof: easy to swap BM25 for ParadeDB later, easy to add cross-encoder reranker
