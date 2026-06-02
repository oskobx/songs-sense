# Language Boost Spec

Tag each passage with its detected language. At query time, boost similarity for passages whose language matches the query's detected language. Preserves cross-lingual matching as a fallback but prefers same-language results.

## Goal

After implementing: Polish queries return Polish-language songs prominently (Sentino, Malik Montana, etc.), with English songs as a backup signal. Same for German.

## Schema change

Add column to `passages`:

```sql
ALTER TABLE passages ADD COLUMN language TEXT;
CREATE INDEX passages_language_idx ON passages (language);
```

Values: ISO 639-1 codes — `'en'`, `'pl'`, `'de'`, `'es'`, `'ko'`, etc., or `'unknown'` for ambiguous.

## Step 1: Detect language per passage (one-time pass)

Script: `src/embeddings/tag_passage_languages.py`

For each passage with `language IS NULL`:
1. Use `lingua-py` detector restricted to `[ENGLISH, POLISH, GERMAN, SPANISH, FRENCH, ITALIAN, PORTUGUESE, KOREAN, JAPANESE]`
2. Pass `passage_text` to `detect_language_of()`
3. Map result to ISO code (`en`, `pl`, `de`, `es`, `fr`, `it`, `pt`, `ko`, `ja`)
4. If detection fails or confidence too low, store `'unknown'`
5. Update DB row

Expected runtime: 5-10 min for 86,152 passages (language detection is fast).

Print stats:
- Passages per language (count + %)
- How many `unknown`

## Step 2: Boost at query time

In `scripts/search.py`, modify the SQL to add a language-match bonus.

Boost approach (in SQL):

```sql
SELECT s.artist, s.title, s.year, s.tier,
       1 - (p.{column} <=> %s::vector) +
       CASE WHEN p.language = %s THEN 0.1 ELSE 0.0 END AS sim,
       p.passage_text, p.language
FROM passages p
JOIN songs s ON p.song_id = s.id
ORDER BY sim DESC
LIMIT %s
```

The `%s` after column is the embedding. The new `%s` is the query language code (`en`, `pl`, `de`, or null if unknown).

**Boost value: 0.1**
- Typical similarity scores are 0.5-0.8
- A boost of 0.1 promotes matching-language passages strongly but doesn't completely dominate
- Tunable later if results look wrong

**No boost if query language is unknown.** Use null and the CASE returns 0 for everything.

## Step 3: Update search.py routing

```python
# After detection:
detected = _detector.detect_language_of(query)

if detected == Language.ENGLISH or detected is None:
    column = "embedding"
    model = bge_base
    query_lang = "en" if detected == Language.ENGLISH else None
else:
    column = "embedding_multi"
    model = bge_m3
    query_lang = detected.name.lower()[:2]  # POLISH -> 'pl', GERMAN -> 'de'

# Pass query_lang to the SQL as the new param
```

## Success criteria

- All 86,152 passages have a non-null `language` value
- Language breakdown roughly matches corpus expectations:
  - English: ~80-90%
  - Polish: ~10-15% (Sentino + Malik + Gang Albanii + any Polish lyrics in other tiers)
  - German: ~1-2% (Rammstein)
  - Other (Spanish, Korean, etc.): ~1-3%
- Sanity queries with boost:
  - English `"feeling lost at night"` → English results dominate (mostly unchanged)
  - Polish `"samotność w mieście"` → Sentino/Malik/Gang Albanii surface in top 5
  - German `"die Nacht ist dunkel"` → Rammstein in top 5

## Known risks

- **Mixed-language passages**: e.g., Rammstein songs with English bridges, Polish rap with English ad-libs. Language detection picks dominant language; minority phrases lost. Acceptable.
- **Short passages**: language detection less accurate on very short text. Mark `unknown` if confidence is low.
- **Boost value tuning**: 0.1 might be too much or too little. Easy to tune after seeing results.