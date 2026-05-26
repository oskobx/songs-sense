# Tier 2: Decade Highlights (~1,000 songs)

Part of the seed list for songs-sense. This tier covers the top songs of each decade from the 1960s through the 2010s. Goal: even era coverage without overweighting any decade.

## Goal

Produce `data/tier_2_decades.csv` with ~800-1,000 unique songs (after dedup against Tier 1) representing the most popular tracks of each decade.

## Per-decade targets (before dedup against Tier 1)

| Decade | Years | Target count |
|---|---|---|
| 1960s | 1960-1969 | 150 |
| 1970s | 1970-1979 | 150 |
| 1980s | 1980-1989 | 170 |
| 1990s | 1990-1999 | 180 |
| 2000s | 2000-2009 | 180 |
| 2010s | 2010-2019 | 170 |

Total before dedup: ~1,000. After dedup against Tier 1: expect 700-900 unique songs.

Note: the 2010s target is intentionally lower because Tier 3 (Recent Popular) will heavily cover 2015-2026.

## Source

**Billboard Year-End Hot 100** — Billboard publishes a "Year-End Hot 100" for every year since 1958. Each year-end chart ranks the 100 songs that performed best across that calendar year (aggregating weekly chart performance).

For each decade, aggregate all year-end charts (10 years × 100 songs = 1,000 candidate song-years per decade), then select the top-N unique songs.

### How to pick top-N per decade

A song's "decade score" is computed by summing inverse ranks across all year-end charts in the decade where the song appeared:

- For each year-end appearance: `points = 101 - rank` (so rank 1 → 100 points, rank 100 → 1 point)
- A song's decade score = sum of points across all year-end appearances in that decade

Then sort by decade score descending and take the top N per decade.

This rewards both peak performance AND sustained presence within the decade:
- A song that hit #1 once gets 100 points
- A song that hit #15, #18, #22, #19 across four years gets 86+83+79+82 = 330 points
- The four-year sustained hit ranks higher than the one-off chart-topper

This produces a corpus of songs that *defined the era* — either by being inescapable for one year or by sustained popularity over multiple — rather than just one-off chart peaks.

If a song appears in year-end charts of TWO different decades (rare — release at decade boundary), assign it to the decade where it has higher total score. Don't double-count.

## Acquisition strategy

Same priority order as Tier 1:
1. Existing Kaggle dataset (search Kaggle for "Billboard Year-End Hot 100")
2. Public GitHub CSV/JSON
3. Wikipedia scrape (each year has a "Billboard Year-End Hot 100 singles of YYYY" page with the full table)
4. Last resort: scraping Billboard directly

Do NOT use ScraperAPI for this tier — sources should be publicly accessible.

## Output schema

CSV at `data/tier_2_decades.csv` with columns:

| Column | Type | Description |
|---|---|---|
| `artist` | string | Primary artist only, cleaned |
| `featured_artists` | string or null | Comma-separated featured artists, parsed |
| `title` | string | Song title, cleaned |
| `year` | int | The year this song appeared on Billboard Year-End Hot 100 (if it appeared in multiple, use the year of best rank) |
| `decade` | string | Decade label: `"1960s"`, `"1970s"`, `"1980s"`, `"1990s"`, `"2000s"`, `"2010s"` |
| `source` | string | Always `"billboard_year_end"` |
| `source_rank` | int | Decade score (sum of inverse ranks across year-end appearances in the decade). Higher = more popular. |
| `tier` | string | Always `"decades"` |

## Cleaning rules

Re-use the shared utilities in `src/corpus/cleaning.py`:
- `parse_featured_artists` — for separating primary from featured artists
- Band allowlist — bands with commas in their name (Earth Wind & Fire, etc.) should NOT be split
- Title parentheticals — keep as-is
- Drop remixes if original is present in the same decade's data
- Drop live versions, demos, acoustic versions if the original is present

## Deduplication

**Within Tier 2:** A song appearing in multiple year-end charts within the decade should result in ONE row per song per decade — with `source_rank` = the best rank achieved.

**Across decades within Tier 2:** A song can appear in multiple decades (rare — e.g. a song released late in a decade that re-charts the next year) — drop the duplicate, keep the entry with the better source_rank.

**Across Tiers (vs Tier 1):** After building Tier 2, dedup against Tier 1:
- Load `data/tier_1_canonical.csv`
- Remove any Tier 2 row where `(normalized_artist, normalized_title)` matches a Tier 1 row
- Use the same normalization as Tier 1's dedup (strip "The " prefix, lowercase, strip whitespace/punctuation for comparison only)

Print stats on how many were removed by Tier 1 dedup.

## Success criteria

- `data/tier_2_decades.csv` exists
- Row count between 700 and 1,000 (after dedup against Tier 1)
- Each decade is represented with roughly its target count (within ±15%)
- No exact duplicates on (artist, title)
- No song appears in both `data/tier_1_canonical.csv` and `data/tier_2_decades.csv` (modulo normalization-equivalence)
- Spot-check: 5 random songs from each decade are real, recognizable songs of that decade
- These specific songs should appear (decade sanity checks):
  - 60s: "Hey Jude" by The Beatles — but wait, this might be in Tier 1. If so, it'd be dedup'd out. Use a less canonical sanity check: "I Heard It Through the Grapevine" by Marvin Gaye
  - 70s: "Stayin' Alive" by Bee Gees
  - 80s: "Billie Jean" by Michael Jackson
  - 90s: "I Will Always Love You" by Whitney Houston
  - 00s: "Hey Ya!" by OutKast
  - 10s: "Uptown Funk" by Mark Ronson (or Bruno Mars)
- Note: any sanity-check songs that happen to be in Tier 1 will be missing from Tier 2 (correct behavior). Verify at least 3 of the 6 are present, others can be in Tier 1.

## Implementation notes

- Code lives in `src/corpus/build_tier_2.py`
- Re-use `src/corpus/cleaning.py` (do NOT duplicate cleaning logic)
- If new shared utilities are needed, add them to `cleaning.py` rather than inlining in build_tier_2.py
- Any new Python dependencies via `uv add`
- Print a summary at end of run:
  - Songs per decade before dedup
  - Songs per decade after dedup against Tier 1
  - Total final count
  - Count of rows with featured artists