# Tier 4: Genre Canonical (~1,500-2,000 songs)

Part of the seed list for songs-sense. This tier captures the *critically-acclaimed and scene-defining* songs across major genres — songs that critics loved or that defined their scenes, even when they didn't chart on Billboard. Counterweight to the Billboard-heavy Tiers 1-3.

## Goal

Produce `data/tier_4_genres.csv` with ~1,500-2,000 unique songs (after cross-tier dedup) covering the canonical works of 10 major genres, with a 50/50 split between all-time canon and last-15-years recent canon within each genre.

## Per-genre source targets (before dedup)

Source ~3,000 candidates total across genres, expecting ~50% to be lost to cross-tier dedup:

| Genre | Source candidates | Expected unique after dedup |
|---|---|---|
| Indie / alternative rock | 500 | ~250 |
| Hip-hop (deep/underground) | 400 | ~150-200 |
| Electronic / dance | 400 | ~250 |
| R&B / soul (non-pop) | 300 | ~120 |
| K-pop | 300 | ~200 |
| Latin (non-pop) | 300 | ~150 |
| Country (non-mainstream / alt-country) | 200 | ~120 |
| Metal | 200 | ~180 |
| Jazz (modern) | 200 | ~180 |
| Folk / singer-songwriter | 200 | ~150 |

Total source candidates: ~3,000. After cross-tier dedup: target 1,500-2,000 unique songs.

## Era weighting

Within each genre, split sourced candidates **50/50** between:

- **All-time canon** — songs that defined the genre across its history, drawn from "Greatest [Genre] Songs of All Time" lists
- **Recent canon (last 15 years, 2010-2026)** — best-of-decade lists, year-end lists from the genre's critical press

This gives historical depth without underweighting recent canon.

## Sources

### Multi-genre sources (cover several genres at once)

**1. Pitchfork year-end and decade lists**
- "Best Songs of [Year]" annual lists, 2003-2026
- "Best Songs of the [Decade]" lists (2000s, 2010s)
- Indie/alt-skewed but covers electronic, hip-hop, R&B, indie, folk
- Available: Pitchfork's site + Wikipedia summaries + various Kaggle compilations

**2. Rolling Stone genre-specific lists**
- "100 Greatest Hip-Hop Songs of All Time"
- "100 Greatest Country Songs"
- "100 Greatest Songs of the 2000s/2010s"
- Etc.
- Available: Wikipedia tables for most

**3. Acclaimed Music aggregated rankings**
- Aggregates rankings across hundreds of publications by genre
- Provides "best of all time per genre" rankings
- Available: acclaimedmusic.net (scrape) or Kaggle datasets

### Genre-specific sources

For each genre, also try at least one genre-specialized source:

| Genre | Specialized source(s) |
|---|---|
| Indie / alt rock | Pitchfork (already covered), NME Best of Decade, Stereogum Best of |
| Hip-hop | Complex, XXL, The Fader, Genius "Best Rap Songs" |
| Electronic / dance | Resident Advisor, Mixmag, Beatport canon, RA Top Tracks of Year |
| R&B / soul | The Fader, Pitchfork R&B sections, Rolling Stone R&B lists |
| K-pop | MelOn year-end charts, K-pop critic lists (Wikipedia compilations) |
| Latin | Billboard Latin year-end (already in Tier 3) + Rolling Stone Latin lists |
| Country (non-mainstream) | No Depression, Saving Country Music, alt-country canon lists |
| Metal | Loudwire "Best Metal Songs", Metal Hammer year-end |
| Jazz (modern) | All About Jazz year-end, JazzTimes, Pitchfork jazz |
| Folk / singer-songwriter | No Depression, Pitchfork folk, Stereogum |

Some of these are hard to scrape — fall back to Wikipedia summaries or Kaggle compilations if direct sources fail.

### Source priority

For each genre, acquire from sources in this order:
1. **Wikipedia summaries** of canonical lists (RS, Pitchfork, etc.) — easiest
2. **Kaggle datasets** if they aggregate the relevant lists — easy if available
3. **acclaimedmusic.net** scrape — works for "best of all time" data
4. **Direct publication scrapes** (Pitchfork, NME, etc.) — last resort, fragile

If a genre's sources fail entirely, log it and proceed with reduced count for that genre.

## Scoring per source

Each source contributes points to a candidate song. Different sources have different authority/trust:

| Source | Weight | Rationale |
|---|---|---|
| Pitchfork year-end / decade lists | 1.0 | Highly influential critical publication |
| Rolling Stone genre lists | 1.0 | Long-running authoritative publication |
| Acclaimed Music aggregated | 1.0 | Aggregates many publications already |
| Genre-specialized publications | 0.7 | Strong within their scene but narrower |
| Wikipedia-derived "Best of" lists | 0.5 | Community-curated, variable quality |

Normalized scoring within each source's list:
```
normalized_score = (LIST_LENGTH + 1 - rank) / LIST_LENGTH
weighted_score = normalized_score × source_weight
```

Combined score for a candidate = sum across all sources where it appeared.

This means a song that appears in BOTH Pitchfork Best of 2018 AND Rolling Stone Best Hip-Hop accumulates more than a song that appears only on one list. Cross-source confirmation = stronger canonical signal.

## Output schema

CSV at `data/tier_4_genres.csv` with columns:

| Column | Type | Description |
|---|---|---|
| `artist` | string | Primary artist only, cleaned |
| `featured_artists` | string or null | Comma-separated featured artists, parsed |
| `title` | string | Song title, cleaned |
| `year` | int or null | Release year if known |
| `genre` | string | Primary genre tag (e.g. `"indie"`, `"hiphop"`, `"electronic"`, `"rnb"`, `"kpop"`, `"latin"`, `"country"`, `"metal"`, `"jazz"`, `"folk"`) |
| `source` | string | Comma-separated list of sources |
| `source_rank` | float | Combined score (higher = stronger canonical signal) |
| `tier` | string | Always `"genre"` |

If a song genuinely spans multiple genres, pick the dominant one based on where it was most prominently listed.

## Cleaning rules

Re-use `src/corpus/cleaning.py`:
- `parse_featured_artists` — for separating primary from featured artists
- Band allowlist — bands with commas in their name
- Title parentheticals — keep as-is
- Drop remixes if original is present
- Drop live versions, demos, acoustic versions if original is present

**IMPORTANT:** Use the FIXED version of `parse_featured_artists` (the one updated for Tier 3, which handles `featuring`, `feat.`, `ft.`, and splits on `and` in addition to `&`, `,`).

If new cleaning utilities are needed for unusual genre source formats (e.g. K-pop Hangul / Romanization, jazz featuring multiple co-leads), add them to `cleaning.py`, not inline.

## Deduplication

**Within Tier 4:** A song appearing in multiple genre lists (e.g. an indie-electronic crossover) should result in ONE row, assigned to its dominant genre, with all sources merged.

**Across Tiers (vs Tiers 1, 2, 3):** After building Tier 4, dedup against all three prior tiers:
- Load `data/tier_1_canonical.csv`, `data/tier_2_decades.csv`, `data/tier_3_recent.csv`
- Remove any Tier 4 row where `(normalized_artist, normalized_title)` matches a row in any prior tier
- Use the same normalization as before

Print stats:
- Songs sourced per genre (before any dedup)
- Songs lost to within-Tier-4 dedup (cross-genre matches)
- Songs lost to dedup against Tier 1, Tier 2, Tier 3 (separately)
- Final per-genre count
- Total final count

## Success criteria

- `data/tier_4_genres.csv` exists
- Total row count between 1,500 and 2,200
- Each of the 10 genres has at least 80 rows in the final output (genres should not collapse to near-zero)
- No exact duplicates on (artist, title)
- No song appears in Tiers 1-3 AND Tier 4 (modulo normalization)
- Spot-check: pick 5 random songs from each genre. Each should be a plausible canonical work of that genre (not a misclassification)
- These specific songs should appear (genre sanity checks — pick songs unlikely to be in Tiers 1-3):
  - Indie: "Two Weeks" by Grizzly Bear OR "Skinny Love" by Bon Iver
  - Hip-hop: "Devil in a New Dress" by Kanye OR "M.A.A.D. City" by Kendrick (without Tier 1 overlap)
  - Electronic: "Strobe" by Deadmau5 OR "Midnight City" by M83
  - R&B/soul: "Cranes in the Sky" by Solange (if not in Tier 1) OR "Nikes" by Frank Ocean
  - K-pop: any major BTS / Blackpink / NewJeans song
  - Latin: "Despacito" if not in Tier 3 OR Bad Bunny canon
  - Country (alt): "Brothers on a Hotel Bed" by Death Cab... wait, that's indie. Try "Murder in the City" by The Avett Brothers
  - Metal: "Black Sabbath" by Black Sabbath OR "Master of Puppets" by Metallica
  - Jazz: any Robert Glasper / Kamasi Washington / Tigran Hamasyan
  - Folk: any Phoebe Bridgers / Big Thief / Bon Iver track
- (Some sanity checks may be absent because they're in Tiers 1-3 already; that's expected. Verify at least 5 of the 10 are present in Tier 4.)

## Implementation notes

- Code lives in `src/corpus/build_tier_4.py`
- Re-use `src/corpus/cleaning.py`
- Factor each source into a helper function (e.g. `_parse_pitchfork_year_end`, `_parse_rolling_stone_hiphop`, `_parse_acclaimed_music`)
- Some sources will need scraping; some Kaggle datasets exist that compile multiple lists already — prefer Kaggle when available
- Genre tagging: each list comes with an implicit genre tag (e.g. "Rolling Stone 100 Greatest Hip-Hop" → genre=`"hiphop"`). When a song appears across genres (rare), prefer the genre of the higher-scoring source.
- Any new Python dependencies via `uv add`
- Print a summary at end of run:
  - Per-genre count after acquisition
  - Per-genre count after within-Tier-4 dedup
  - Lost to dedup against Tier 1, 2, 3 (per tier)
  - Final per-genre count
  - Total final count
  - Count of rows with multiple sources
  - Count of rows with featured artists

## Known risks

- **Source fragility** — many publication sites don't allow scraping. Fall back to Wikipedia summaries when this happens.
- **Genre tagging ambiguity** — Frank Ocean is R&B/indie/hip-hop. Pick dominant genre per list; don't try to multi-tag.
- **K-pop sourcing** — Korean-language source pages may need special handling; consider using Spotify's K-pop curated playlists as a fallback
- **Latin sourcing** — overlaps heavily with Billboard Latin in Tier 3; expect high dedup loss
- **Jazz sourcing** — jazz lists exist but are less commonly compiled on Wikipedia; may need direct Pitchfork/AllAboutJazz scrapes