# Dual-Index Spec: Language-Aware Retrieval

Adds a second embedding index for multilingual queries, with language detection at query time routing to the appropriate index.

## Goal

Preserve the current English retrieval quality (bge-base-en-v1.5) while adding strong multilingual support for Polish and German queries via bge-m3. Other languages (Spanish, Korean, etc.) get reasonable fallback coverage via the same multilingual index.

## Design

### Two embedding columns

```
passages
├── embedding         vector(768)   -- bge-base-en-v1.5 (English-optimized)
└── embedding_multi   vector(1024)  -- bge-m3 dense mode (multilingual)
```

Both columns populated for all 86,152 passages. HNSW index on each.

### Query routing

```
1. User submits query
2. Detect query language with lingua-py (restricted to English/Polish/German)
3. Route:
   - English → embedding column (bge-base-en-v1.5)
   - Polish → embedding_multi column (bge-m3)
   - German → embedding_multi column (bge-m3)
   - Detection fails or ambiguous → embedding column (English fallback)
4. Return top-k results
```

**Priority languages**: English, Polish, German. These are the languages with meaningful representation in the corpus:
- English: ~7,500 songs from canonical / decades / recent / viral tiers
- Polish: ~1,200 songs from Sentino + Malik Montana + Gang Albanii
- German: ~150 songs from Rammstein

**Other languages** (Spanish, Korean, French, etc.) exist in smaller quantities. They are not explicitly routed but bge-m3 handles them reasonably as a fallback when users supply queries in those languages.

**Ambiguous queries** (single English-sounding words, undetected languages): fall back to English. English coverage is largest in the corpus, so English fallback maximizes hit rate for ambiguous cases.

In code:

```python
from lingua import Language, LanguageDetectorBuilder

detector = (
    LanguageDetectorBuilder
    .from_languages(Language.ENGLISH, Language.POLISH, Language.GERMAN)
    .build()
)

def route_query(query: str) -> str:
    """Returns 'english' or 'multilingual'."""
    detected = detector.detect_language_of(query)
    if detected == Language.ENGLISH or detected is None:
        return "english"
    return "multilingual"
```

Restricting the detector to {English, Polish, German} via `from_languages()` is a small accuracy boost: lingua won't try to classify the query as e.g. Hungarian or Albanian, which avoids false positives on short queries.

Note: this is **query-time routing only**. Passages are always embedded with both models — passage language doesn't affect indexing.

## Implementation steps

### Step 1: Add new column + library deps

```sql
ALTER TABLE passages ADD COLUMN embedding_multi vector(1024);
```

```bash
uv add lingua-language-detector FlagEmbedding
```

(Note: `lingua-py` is the project name; the pip package is `lingua-language-detector`.)

### Step 2: Embed all passages with bge-m3

- Script: `src/embeddings/embed_multilingual.py`
- Load bge-m3 model via FlagEmbedding (~2.3 GB download, one-time)
- Process passages in batches of 32
- Use MPS if available
- For passages: `model.encode(passages, batch_size=32, max_length=512)['dense_vecs']`
- bge-m3 embeddings are normalized by default
- Resumable via `WHERE embedding_multi IS NULL` check
- Expected runtime: ~30-50 min on MPS

### Step 3: Build HNSW index on the new column

```sql
CREATE INDEX passages_embedding_multi_hnsw_idx
ON passages
USING hnsw (embedding_multi vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

Build time: ~5-10 min.

### Step 4: Update the search script with routing

`scripts/search.py` becomes:

```python
from lingua import Language, LanguageDetectorBuilder

detector = (
    LanguageDetectorBuilder
    .from_languages(Language.ENGLISH, Language.POLISH, Language.GERMAN)
    .build()
)

# At query time:
detected = detector.detect_language_of(query)
if detected == Language.ENGLISH or detected is None:
    model_to_use = bge_base
    column = "embedding"
else:
    model_to_use = bge_m3
    column = "embedding_multi"

# encode + query the appropriate column
```

Both models stay loaded in memory for fast switching.

### Step 5: Sanity tests

Run three sanity queries — one per priority language — and verify:

| Query | Language | Expected character |
|---|---|---|
| `"feeling lost at night"` | English | Iron Maiden, SZA, Depeche Mode, etc. — dark/lonely/night themes |
| `"samotność w mieście"` | Polish | Sentino, Malik Montana songs about urban loneliness |
| `"die Nacht ist dunkel"` | German | Rammstein songs about night/darkness |

Print the detected language for each query alongside the results, so we can verify routing is working correctly.

## Success criteria

- New column `embedding_multi` populated for all 86,152 passages (no nulls)
- All vectors are 1024-dim
- Average norm ≈ 1.0
- HNSW index `passages_embedding_multi_hnsw_idx` exists
- Language detection works for English / Polish / German on test queries
- Sanity queries return thematically right results in each language
- Sanity queries return results predominantly in the matching language (not cross-language pollution)
- English query performance unchanged (regression check vs bge-base baseline)

## Why this design

- **Quality**: bge-m3 is current state-of-the-art for multilingual retrieval (Aug 2024 release)
- **Preserves English quality**: bge-base-en-v1.5 stays as the English path, no quality regression
- **Storage acceptable**: extra 1024 × 4 = 4 KB per passage = ~350 MB additional storage. Fine.
- **Operational simplicity**: same database, same schema, just an extra column + index
- **Future-proof**: if we later want to fine-tune the multilingual model in Phase 4, it's a drop-in swap

## Known risks

- **bge-m3 model download**: ~2.3 GB. One-time on first run, then cached.
- **MPS / FlagEmbedding compatibility**: FlagEmbedding may need `device='mps'` set explicitly. May fall back to CPU if MPS not supported by the lib's wrapper.
- **Slower encoding**: bge-m3 ~2-3x slower than bge-base per passage due to larger model. Full pass ~30-50 min vs original ~15 min.
- **Some queries are ambiguous language**: very short queries ("hope", "love") may detect inconsistently. The fallback (route to English / bge-base when unsure) is reasonable.
