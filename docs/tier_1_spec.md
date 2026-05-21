# Tier 1: All-Time Canonical (~1,000 songs)

Part of the seed list for songs-sense. This tier covers the "everyone knows these" songs across all eras — cultural literacy floor.

## Goal

Produce `data/tier_1_canonical.csv` with ~900-1,100 unique songs that constitute the canonical pop/rock/hip-hop/soul/country songs of the last 70 years.

## Sources (priority order)

1. **Rolling Stone "500 Greatest Songs of All Time" (2021 revision)** — ~500 songs, broad cultural coverage, rock-biased
2. **Billboard "Greatest of All Time Hot 100 Songs"** — ~600 songs, chart-position-weighted, more pop/recent
3. **IFPI Global Best-Selling Singles** (optional) — ~200 songs, international flavor

After dedup across sources, expect 900-1,100 unique songs.

## Acquisition strategy

For each source, prefer in this order:
1. Existing Kaggle dataset (search Kaggle for the list name)
2. Public GitHub CSV/JSON
3. Wikipedia scrape (Wikipedia hosts both the Rolling Stone 500 and Billboard GOAT lists as articles with full tables)
4. Last resort: scraping the publication's own page

Do NOT use ScraperAPI for this tier — these sources are all publicly accessible without anti-bot challenges.

## Output schema

CSV at `data/tier_1_canonical.csv` with columns:

| Column | Type | Description |
|---|---|---|
| `artist` | string | Primary artist only, cleaned |
| `featured_artists` | string or null | Comma-separated list of featured artists, e.g. `"Kid Cudi, Lloyd"`. Null if no features. |
| `title` | string | Song title, cleaned |
| `year` | int or null | Release year if known, else null |
| `source` | string | Comma-separated: `rs500`, `billboard_goat`, `ifpi` |
| `source_rank` | int or null | Best (lowest) rank across sources, or null |
| `tier` | string | Always `"canonical"` |

## Cleaning rules

These apply to all tiers — implement them in a shared utility in `src/corpus/cleaning.py`:

- **Artist normalization**: strip leading/trailing whitespace, normalize curly quotes to straight. Do NOT lowercase (preserve "AC/DC", "M.I.A.").
- **Featured artists**: Parse out featured artists from raw artist strings into a separate `featured_artists` column.
  - Patterns to recognize: `feat.`, `ft.`, `featuring`, `with`, `&` (when after a comma), comma-separated lists
  - `"Drake (feat. Kid Cudi & Lloyd)"` → artist=`"Drake"`, featured_artists=`"Kid Cudi, Lloyd"`
  - `"Drake, Kid Cudi, Lloyd"` → artist=`"Drake"`, featured_artists=`"Kid Cudi, Lloyd"` (heuristic: comma-separated with no explicit "feat" → assume features)
  - `"Simon & Garfunkel"` → artist=`"Simon & Garfunkel"`, featured_artists=null (single `&` with no other separators = band name)
  - `"AC/DC"` → artist=`"AC/DC"`, featured_artists=null (no separators)
  - `"Lil Nas X feat. Billy Ray Cyrus"` → artist=`"Lil Nas X"`, featured_artists=`"Billy Ray Cyrus"`
  - Title stays unchanged regardless
- **Title parentheticals**: keep them — "Hurt (Cash version)" stays as-is.
- **Remixes**: drop remix versions when the original is also in the list. Keep remix only if it's notably distinct (e.g. "Old Town Road (Remix)" — keep both because the remix is the famous version).
- **Live versions, demos, acoustic versions**: drop if original is present.

## Deduplication

A song appearing in multiple sources should become ONE row with `source` as a comma-separated list and `source_rank` as the best (lowest) rank across sources.

Fuzzy matching for dedup:
- Strip "The " prefix from artist for comparison only (so "Beatles" matches "The Beatles")
- Lowercase for comparison only (not for storage)
- Strip whitespace and punctuation for comparison

Featured artists are NOT part of the dedup key — the same song with different listed featured artists should still merge into one row (keep the more complete `featured_artists` value).

## Success criteria

- `data/tier_1_canonical.csv` exists
- Row count between 900 and 1,100
- No exact duplicates on (artist, title)
- These specific songs are present:
  - "Bohemian Rhapsody" by Queen
  - "Like a Rolling Stone" by Bob Dylan
  - "Smells Like Teen Spirit" by Nirvana
  - "Respect" by Aretha Franklin
  - "Hey Jude" by The Beatles
- Spot-check: random 20 rows are all real, recognizable songs
- At least a few rows have non-null `featured_artists` (sanity check the parsing works)

## Implementation notes

- Code lives in `src/corpus/build_tier_1.py`
- Shared cleaning utilities in `src/corpus/cleaning.py`
- Any new Python dependencies via `uv add`
- Print a summary at end of run: source counts, dedup count, final count, count of rows with featured artists