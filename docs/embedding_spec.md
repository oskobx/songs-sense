# Substep 2b: Passage Embedding Spec

Encodes each of the 86,152 passages with `bge-base-en-v1.5` and stores the 768-dim vector in the `passages.embedding` column. After this, basic semantic retrieval works end-to-end.

## Goal

For each passage with NULL embedding:
1. Pass `passage_text` through `bge-base-en-v1.5`
2. Get a 768-dim normalized vector
3. Store in `embedding` column (pgvector type)

After full pass: build an HNSW index on `embedding` for fast similarity queries.

## Model

**`BAAI/bge-base-en-v1.5`**
- 768 dimensions
- Trained for English semantic search
- Multilingual coverage is partial — works "OK-ish" on Polish/German/Spanish (better than chance, worse than dedicated multilingual models)
- For this corpus (mostly English, some Polish/German/Korean/Spanish), bge-base is the right tradeoff: clean baseline, easy to fine-tune in Phase 4

## Library

**`sentence-transformers`** for clean, batched encoding.

```python
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('BAAI/bge-base-en-v1.5')
embeddings = model.encode(passages, batch_size=32, normalize_embeddings=True, show_progress_bar=True)
```

Key flags:
- `normalize_embeddings=True` — output unit-length vectors, makes cosine similarity equivalent to inner product (faster)
- `batch_size=32` — good for Mac MPS / CPU balance
- `show_progress_bar=True` — useful for the 30-60 min run

## Device

Auto-detect:
1. **MPS** (Apple Silicon GPU) if `torch.backends.mps.is_available()` — fastest on M-series Macs
2. **CUDA** if available (won't apply here, Oskar's on Mac)
3. **CPU** fallback

Expected runtime:
- MPS: 30-60 min for 86k passages
- CPU: 3-6 hours

## Database write strategy

Don't write one passage at a time (too slow). Strategy:
1. Pull 1000-passage batches from DB where `embedding IS NULL`
2. Encode the batch (32 at a time via sentence-transformers internal batching)
3. UPDATE each row with its embedding
4. Repeat until no NULL embeddings remain

Use `psycopg.copy()` or `executemany` for batch updates. pgvector accepts vectors as lists or numpy arrays via the `psycopg` adapter.

## Index creation

After all embeddings are populated, create an HNSW index:

```sql
CREATE INDEX passages_embedding_hnsw_idx ON passages
USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

Parameters:
- `m = 16`: connections per node (default; good speed/recall tradeoff)
- `ef_construction = 64`: search width during build (default; higher = slower build but better recall)
- `vector_cosine_ops`: cosine distance operator (matches normalize_embeddings=True)

Index build time: ~5-10 min for 86k vectors.

## Resumability

Same pattern as Phase 1:
- Each iteration: `SELECT id, passage_text FROM passages WHERE embedding IS NULL LIMIT 1000`
- Encode, UPDATE
- Re-running picks up where it left off via NULL check

No external state file needed.

## Output stats

Print after completion:
- Total passages processed
- Total embedding time
- Throughput (passages/sec)
- Avg vector norm (should be ~1.0 since normalized)
- Verification: random sample of 5 passages with their embedding[:5] preview
- Index build time

## Success criteria

- 100% of passages have non-null `embedding`
- All vectors are 768-dim
- Average norm ≈ 1.0 (within 1e-5)
- HNSW index exists and is queryable
- Sanity test: embed a query like "feeling lost at night" and return top-5 passages — they should look semantically relevant

## Implementation

- Script: `src/embeddings/embed_passages.py`
- Function: `embed_all_passages()` does the main loop
- After main loop: `create_hnsw_index()` runs the CREATE INDEX
- Use `tqdm` for progress (already a sentence-transformers dependency)

Dependencies to add:
- `uv add sentence-transformers torch numpy`
- `uv add pgvector` (the Python pgvector adapter for psycopg)

## Known risks

- **MPS quirks**: occasional model load issues on macOS, fixed by setting `PYTORCH_ENABLE_MPS_FALLBACK=1`. If MPS fails, fall back to CPU.
- **Memory pressure**: bge-base + 86k passages + embeddings buffer ≈ 2-3 GB RAM. Should fit fine on 16GB+ Mac.
- **Power throttling**: M-series Macs throttle MPS workloads when on battery. **Run on AC power for consistent speed.**

## Why this spec

- bge-base-en-v1.5 chosen per master plan (right tradeoff for English-skewed corpus + lyric search)
- 768 dim is bge-base's native dim — matches `vector(768)` column type already in schema
- HNSW index is pgvector's fastest approximate index; the gold standard for sub-100ms top-k queries
- Cosine distance matches normalized embeddings, the standard for sentence-transformers