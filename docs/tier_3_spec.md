# Tier 3: Recent Popular (2015-2026, ~5,000 songs)

Part of the seed list for songs-sense. This is the largest and most demo-critical tier — most user queries will land on songs from the last decade. Coverage must be broad across genres and across the Billboard/Spotify chart split (US radio vs global streaming).

## Goal

Produce `data/tier_3_recent.csv` with ~4,500-5,000 unique songs (after dedup against Tiers 1 and 2) representing the most popular tracks of each year from 2015 through 2026, drawing from multiple chart sources to capture genre and international diversity.

## Per-year targets

- **~420 songs per year × 12 years (2015-2026) = 5,040 candidate slots before dedup**
- After dedup against earlier tiers: expect 4,500-5,000 unique songs.

Notes:
- 2026 may be partial (year not complete). Use whatever year-end data exists; scale target down if needed. If no 2026 year-end data is available yet, target ~250 songs for 2026 from available H1 sources.
- 2015-2019 will have some overlap with Tier 2's 2010s decade highlights — that's expected and handled by dedup.

## Sources

For each year, acquire data from as many of the following as feasible. Mandatory sources MUST succeed; bonus sources are best-effort.

### Mandatory sources

**1. Billboard Year-End Hot 100** — same as Tier 2
- Format: 100 songs per year, ranked 1-100
- Source: Wikipedia "Billboard Year-End Hot 100 singles of YYYY" pages

**2. Billboard Year-End genre charts** — 6 genre charts per year
- Hot Rap Songs (top 25-50/year)
- Hot Country Songs (top 25-50/year)
- Hot Latin Songs (top 25-50/year)
- Hot R&B / Hip-Hop Songs (top 25-50/year)
- Hot Dance/Electronic Songs (top 25-50/year)
- Hot Rock & Alternative Songs (top 25-50/year)
- Source: Wikipedia "Billboard Year-End Hot [Genre] Songs of YYYY" pages
- ~150-200 songs/year total across genres (with internal overlap)
- Coverage note: some genre charts may not exist for every year; that's OK, skip those gracefully

**3. Spotify Year-End Top Songs (Global)**
- Format: ~100-200 songs per year
- Sources to try in order:
  1. Wikipedia "List of most-streamed songs on Spotify" annual lists
  2. Kaggle datasets (search "Spotify Year-End")
  3. spotifycharts.com archives (web archive may be needed for older years)
- Coverage best from 2019 onwards; 2015-2018 may need fallback to Spotify "Streamed of all time as of YYYY" approximations

**4. Spotify Year-End UK**
- Format: top 50-100 songs per year
- Sources: same as Spotify Global, often appears on the same Wikipedia page or Spotify country page

### Bonus sources (try, but don't block if they fail)

**5. Spotify Year-End US** — overlaps heavily with Billboard, but catches streaming-only artists
- Try Wikipedia or Kaggle; skip if not easily findable
- Implementation note: if available, set `source` to include `spotify_us`. If not, log "Spotify US unavailable for year YYYY" and proceed.

**6. Apple Music Year-End (Top Songs of the Year)**
- Best available 2019 onwards
- Try Wikipedia, Apple Music's published year-end playlists
- If unavailable for a given year, log and skip

**7. YouTube Music Year-End Top Songs**
- Coverage on Wikipedia is patchy
- If easy to find, include; if not, log and skip after one good attempt

### Union strategy per year

For each year:

1. Acquire all available sources for that year
2. Merge by `(normalized_artist, normalized_title)` — same song from multiple sources becomes ONE candidate with all sources noted
3. Compute combined score per candidate (see scoring below)
4. Sort by combined score descending
5. Take top 420 for the year (or all candidates if fewer than 420 — should be plenty)

### Scoring per source

Each source contributes a normalized, weighted score based on the song's rank within that source's chart:

```
normalized_score = (CHART_LENGTH + 1 - rank) / CHART_LENGTH    # always in (0, 1]
weighted_score = normalized_score × source_weight
```

Source weights:

| Source | Weight | Notes |
|---|---|---|
| Billboard Hot 100 | 1.0 | Primary signal — editorially-curated US cultural reference |
| Billboard genre charts (Rap, Country, Latin, R&B, Dance/Electronic, Rock/Alternative) | 1.0 | Primary signal — captures genre-specific popularity |
| Spotify Global | 0.5 | Supplementary — global streaming breadth |
| Spotify UK | 0.5 | Supplementary — UK market signal |
| Spotify US (bonus) | 0.4 | Supplementary, often overlaps Billboard |
| Apple Music (bonus) | 0.4 | Supplementary |
| YouTube Music (bonus) | 0.4 | Supplementary |

Combined score for a song = sum of weighted scores across all sources it appeared in.

**Rationale:** Billboard is the established cultural-popularity reference (aggregates radio, sales, streaming with editorial methodology). Other charts represent single-platform listening behavior — useful for breadth and international coverage, but lower-weight signals. Normalizing within each chart before weighting ensures that the weighting reflects source authority, not chart length.

Example outcomes:
- A song at #1 on Billboard Hot 100, absent elsewhere → score 1.0
- A song at #1 on Spotify Global, absent elsewhere → score 0.5
- A song at #1 on Billboard AND #1 on Spotify Global AND #20 on Apple → 1.0 + 0.5 + 0.4 × 0.81 ≈ 1.82
- A song at #50 on Billboard genre chart only → 1.0 × 0.02 = 0.02 (small but nonzero — reflects narrow scope)

Sort candidates per year by combined score descending, take top 420.

## Output schema

CSV at `data/tier_3_recent.csv` with columns:

| Column | Type | Description |
|---|---|---|
| `artist` | string | Primary artist only, cleaned |
| `featured_artists` | string or null | Comma-separated featured artists, parsed |
| `title` | string | Song title, cleaned |
| `year` | int | Year of strongest chart appearance |
| `source` | string | Comma-separated list of all sources the song appeared in (e.g. `"billboard_hot_100, billboard_rap, spotify_global"`) |
| `source_rank` | float | Combined score across all sources. Higher = more popular. |
| `tier` | string | Always `"recent"` |

## Cleaning rules

Re-use `src/corpus/cleaning.py`:
- `parse_featured_artists` — for separating primary from featured artists
- Band allowlist — bands with commas in their name (Earth Wind & Fire, etc.) should NOT be split
- Title parentheticals — keep as-is
- Drop remixes if original is present in the same year's data
- Drop live versions, demos, acoustic versions if the original is present

If new cleaning utilities are needed (e.g. Spotify's specific artist string format with no `feat.`), add them to `cleaning.py`, not inline.

## Deduplication

**Within each year:** A song appearing in multiple sources for the same year is ONE row with combined score and source list.

**Across years within Tier 3:** A song appearing in multiple years (common — long-running hits like "Heat Waves") → keep only the row from the year with the higher combined score. Other year entries are dropped.

**Across Tiers (vs Tiers 1 and 2):** After building Tier 3, dedup against both:
- Load `data/tier_1_canonical.csv` AND `data/tier_2_decades.csv`
- Remove any Tier 3 row where `(normalized_artist, normalized_title)` matches a row in either Tier 1 or Tier 2
- Use the same normalization as before (strip "The " prefix, lowercase, strip whitespace/punctuation for comparison only)

Print stats separately:
- How many rows were removed by Tier 1 overlap
- How many rows were removed by Tier 2 overlap

## Success criteria

- `data/tier_3_recent.csv` exists
- Row count between 4,500 and 5,000
- Each year 2015-2025 has at least 300 rows (after dedup); 2026 may have fewer
- No exact duplicates on (artist, title)
- No song appears in Tier 1 OR Tier 2 AND Tier 3 (modulo normalization)
- At least 500 rows have multiple sources in their `source` column (verifies union logic)
- At least 200 rows have a non-Billboard source (e.g. only on Spotify) — verifies Spotify integration works
- Spot-check: 10 random rows from 2022-2024 are all real, recognizable songs
- These specific songs should appear (era sanity checks):
  - "Levitating" by Dua Lipa (2020-2021)
  - "Heat Waves" by Glass Animals (2021-2022)
  - "Anti-Hero" by Taylor Swift (2022-2023)
  - "Flowers" by Miley Cyrus (2023)
  - "Espresso" by Sabrina Carpenter (2024)
- Genre diversity check: among the top 100 rows by source_rank, at least 5 distinct genres represented (pop, hip-hop, R&B, country, Latin, K-pop, electronic, rock) — eyeball this

## Implementation notes

- Code lives in `src/corpus/build_tier_3.py`
- Re-use `src/corpus/cleaning.py`
- For each source, factor out the parsing into a helper function (e.g. `_parse_billboard_year_end`, `_parse_spotify_global`, `_parse_apple_music`) so it's clear which source is being handled
- Any new Python dependencies via `uv add`
- If a bonus source (Apple, YouTube, Spotify US) fails to parse cleanly for a year, log a warning but DO NOT raise — proceed with the data you have
- 2026 partial-year handling: if year-end data isn't out yet for a source, use mid-year/H1 data if available; otherwise skip that source for 2026
- Print a summary at end of run:
  - Per-year counts from each source (rows shown)
  - Per-year final count after union + top-420 cut
  - Per-year final count after dedup against Tier 1 and Tier 2
  - Total final count
  - Count of multi-source rows (verifies union)
  - Count of non-Billboard-only rows (verifies international coverage)
  - Count of rows with featured artists
  - Which bonus sources succeeded for each year

## Known risks

- **Spotify 2015-2018 coverage** may be weaker than 2019+. Plan to use Kaggle datasets as fallback.
- **Spotify 2026 doesn't exist yet** (year not over). Document and proceed.
- **Apple Music** historical data is fragmented. Likely partial coverage at best.
- **YouTube Music** historical data is least accessible. Most likely to fail.
- **Wikipedia formatting** — same issues as Tier 2; tables vary by year. Reuse robust parsing from `build_tier_2.py`.
- **Spotify featured-artist format** differs from Billboard. Existing comma-heuristic should handle, but verify on a Spotify sample early.