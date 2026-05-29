# Tier 5: Viral / TikTok Era (~400-500 songs)

Part of the seed list for songs-sense. This tier captures songs that broke through social media (primarily TikTok) from 2019-2026, including back-catalog tracks that resurfaced years after their original release. Counterweight to Billboard's chart-based selection in Tiers 1-3.

## Goal

Produce `data/tier_5_viral.csv` with ~400-500 unique songs (after cross-tier dedup) representing the songs that defined the social media era — what people actually heard scrolling Instagram/TikTok/Reels, regardless of whether those songs charted.

## Time scope

**2019-2026** (8 years). 2019 has spottier data; 2020-2024 is the heart; 2025-2026 may be partial.

## Target counts

| Year | Source candidates | Notes |
|---|---|---|
| 2019 | 50 | Pre-pandemic, limited TikTok-specific data |
| 2020 | 75 | Pandemic era — TikTok exploded |
| 2021 | 100 | Peak TikTok virality |
| 2022 | 100 | |
| 2023 | 100 | |
| 2024 | 100 | |
| 2025 | 75 | |
| 2026 | 50 | Partial year |

Total source candidates: ~650. After cross-tier dedup: target 400-500 unique songs.

Expect heavy dedup loss against Tier 3 (~40-50%) — many viral TikTok songs eventually charted on Billboard. Tier 5's value is in the *non-overlapping* fraction: songs that went viral but didn't chart, or charted in different countries/segments.

## Sources

### Primary sources (try these first)

**1. "Year on TikTok" annual recaps**
- TikTok publishes annual top tracks lists; well-summarized on Wikipedia and in music press
- Search for "Year on TikTok 2020", "Year on TikTok 2021", etc.
- Each typically lists 50-100 most-used sounds/songs
- Available via Wikipedia summaries, TikTok's own newsroom posts (scrape-friendly), or Kaggle compilations

**2. Spotify Viral 50 / Viral charts**
- Spotify maintains a separate "Viral" chart distinct from "Top" chart
- For each year, sample the year's most-appeared songs on the Viral chart
- Available via Spotify Web API (use the credentials already in `.env`) or via chart-tracking sites like kworb.net (which scrapes Spotify data)

**3. Billboard "Hot Trending Songs" year-end charts**
- Billboard launched this chart in 2021, social-media-driven
- Wikipedia has year-end lists for 2021-2024

### Secondary sources (use where useful)

**4. Music publication year-end "viral songs" articles**
- Variety, Billboard, Rolling Stone, NME publish annual "top viral songs of YYYY" lists
- Variable structure — Wikipedia summarizes some of these

**5. "Songs that went viral on TikTok" Wikipedia category/list**
- Wikipedia maintains an article tracking notable songs that gained popularity via TikTok
- Cross-reference with year tags for assignment

### Sources NOT to use

- **Direct TikTok scraping** — hostile to scraping, rate-limited heavily, breaks frequently
- **Hardcoded song lists from model knowledge** — same rule as Tier 4. Songs must come from scraped sources.

## Acquisition strategy

Same anti-spiral approach as Tier 4:
1. Start with ONE source, ONE year — get end-to-end scraping working
2. Show the first 20 rows for verification
3. Then expand to other years/sources

Prefer sources in this order:
1. Wikipedia "Year on TikTok" summaries (most reliable structured data)
2. Spotify Viral via Spotify API (clean data, requires existing credentials)
3. Billboard Hot Trending year-end via Wikipedia (good for 2021-2024)
4. Music publication articles (variable structure, fallback)

If a year has thin data, accept lower count rather than fabricating.

## Scoring per source

Each source contributes a normalized weighted score:

```
normalized_score = (LIST_LENGTH + 1 - rank) / LIST_LENGTH
weighted_score = normalized_score × source_weight
```

Source weights:

| Source | Weight | Rationale |
|---|---|---|
| Year on TikTok official | 1.0 | TikTok's own data on what dominated their platform |
| Spotify Viral chart | 0.8 | Strong streaming-virality signal |
| Billboard Hot Trending year-end | 0.7 | Social-media chart but Billboard-flavored |
| Music publication year-end | 0.5 | Editorial picks, more subjective |

Combined score = sum across all sources where the song appeared.

## Output schema

CSV at `data/tier_5_viral.csv` with columns:

| Column | Type | Description |
|---|---|---|
| `artist` | string | Primary artist only, cleaned |
| `featured_artists` | string or null | Comma-separated featured artists |
| `title` | string | Song title, cleaned |
| `year` | int | Year of viral peak (NOT original release year — many will be back-catalog) |
| `original_year` | int or null | Original release year if known and different from viral year |
| `source` | string | Comma-separated source list |
| `source_rank` | float | Combined score, higher = more viral |
| `tier` | string | Always `"viral"` |

Note: `original_year` is new for this tier. A song like "Running Up That Hill" should have `year=2022` (viral) and `original_year=1985` (release). This signals back-catalog virality clearly.

## Cleaning rules

Re-use `src/corpus/cleaning.py` — use the FIXED version with `featuring` handling.

Special considerations for this tier:
- **Sped-up / slowed-down versions** — drop these if the original is in the corpus. "Cupid (Sped Up)" → drop if "Cupid" by FIFTY FIFTY is present. Only keep the sped-up version if it's the *primary* viral version (rare but happens).
- **Remix versions** — same as other tiers: drop remix if original is present, unless the remix is the famous version.

## Deduplication

**Within Tier 5:** Same song appearing across multiple sources in the same year → one row, merged sources.

**Across years within Tier 5:** A song going viral multiple years (rare) → keep the year with the higher combined score.

**Across Tiers (vs Tiers 1, 2, 3, 4):** After building Tier 5, dedup against all four prior tiers.
- Use the same normalization as before
- Print stats per prior tier

Expect ~40-50% dedup loss to Tier 3 (recent popular) specifically — TikTok songs that charted on Billboard are already there.

## Success criteria

- `data/tier_5_viral.csv` exists
- Row count between 400 and 600
- At least 4 of the 8 years have ≥50 rows (years can be uneven; some viral years are stronger)
- No exact duplicates on (artist, title)
- No song appears in Tiers 1-4 AND Tier 5 (modulo normalization)
- At least 20 rows have `original_year` more than 3 years before `year` (back-catalog virality signal — proves the tier is doing its job)
- Spot-check: 10 random rows are real, recognizable viral songs of their era
- These specific viral phenomena should appear (sanity checks for back-catalog virality):
  - "Running Up That Hill" by Kate Bush (1985, viral 2022 — Stranger Things)
  - "Murder On the Dancefloor" by Sophie Ellis-Bextor (2001, viral 2024 — Saltburn)
  - "Dreams" by Fleetwood Mac (1977, viral 2020 — cranberry-juice-skateboard video)
  - "Cupid" by FIFTY FIFTY (2023 — K-pop TikTok virality)
- (Some sanity checks may be absent if Tier 1/3 already covered them; that's expected. Verify at least 2 of 4 are present.)

## Implementation notes

- Code lives in `src/corpus/build_tier_5.py`
- Re-use `src/corpus/cleaning.py`
- Factor each source into a helper function
- The `original_year` column is NEW — for sources that don't provide release year, leave it null. Don't try to look it up via API for every row (would explode scope).
- Any new Python dependencies via `uv add`
- Print summary at end of run:
  - Per-year counts from each source
  - Per-year counts after union
  - Per-year counts after cross-tier dedup (broken down by which tier did the deduping)
  - Total final count
  - Count of back-catalog virality rows (where `original_year` is meaningfully before `year`)
  - Count of rows with featured artists

## Known risks

- **TikTok data fragility** — TikTok's own publications are inconsistent year-to-year. Some years had detailed lists; others just blog posts. Wikipedia coverage varies.
- **Sped-up version explosion** — many TikTok-viral tracks have multiple sped-up/slowed variants. Aggressive dedup may be needed.
- **Heavy Tier 3 overlap** — most successful TikTok songs also chart on Billboard. Expect ~40-50% dedup loss; that's normal and OK.
- **2026 partial data** — year isn't over, most year-end recaps won't exist yet. Accept thin coverage.
- **Hardcoding temptation** — if scrapeable sources are thin, do NOT fall back to hardcoded song lists. Accept a smaller final count instead.