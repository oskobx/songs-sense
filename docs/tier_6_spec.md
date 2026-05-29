# Tier 6: Personal Favorites (~800-1,000 songs)

Part of the seed list for songs-sense. This tier captures the full discographies of 8 personally-chosen artists, adding depth and flavor that the chart-driven Tiers 1-5 miss. Includes album tracks, B-sides, deep cuts, EP-only songs, mixtape tracks — everything releaseable that's in Genius's database.

## Goal

Produce `data/tier_6_favorites.csv` with ~800-1,000 unique songs (after cross-tier dedup) covering the complete catalogs of 8 selected artists.

## The 8 artists

| Artist | Genre | Era | Genius search |
|---|---|---|---|
| Sentino | Polish rap | 2000s-2020s | "Sentino" |
| Malik Montana | Polish rap | 2010s-2020s | "Malik Montana" |
| Gang Albanii | Polish satirical rap | 2010s-2020s | "Gang Albanii" |
| Lil Peep | Emo rap | 2010s | "Lil Peep" |
| Lil Uzi Vert | Rap | 2010s-2020s | "Lil Uzi Vert" |
| Rammstein | German industrial metal | 1990s-2020s | "Rammstein" |
| Crystal Castles | Electronic | 2000s-2010s | "Crystal Castles" |
| Depeche Mode | Synth-pop / rock | 1980s-2020s | "Depeche Mode" |

## Per-artist target

Roughly **120 songs per artist** = ~960 candidates total. Actual counts will vary:
- Lil Uzi Vert, Depeche Mode, Rammstein: likely 150+ available
- Sentino, Malik Montana, Lil Peep: likely 80-150
- Gang Albanii, Crystal Castles: likely 50-100

After cross-tier dedup against Tiers 1-5: expect 10-30% loss for the bigger mainstream artists (Lil Uzi Vert charting hits will already be in Tier 3). Less loss for the Polish, niche, and electronic artists.

Final target: 800-1,000 unique songs.

## Source

**Genius API** as the primary source.

The flow per artist:
1. Search Genius for the artist → get `artist_id`
2. Use `/artists/{id}/songs` endpoint with pagination → enumerate all songs
3. Filter to songs where this artist is the *primary* artist (Genius returns songs where they're featured too — drop those for Tier 6, they'd be in the original artist's catalog)
4. For each song: extract `title`, `release_date_components.year`, `id`, plus any metadata Genius returns (album, features)

Polish artist coverage on Genius is partial — accept 60-80% coverage for those three artists. Document in output stats.

## Output schema

CSV at `data/tier_6_favorites.csv` with columns:

| Column | Type | Description |
|---|---|---|
| `artist` | string | Primary artist name (one of the 8) |
| `featured_artists` | string or null | Comma-separated featured artists |
| `title` | string | Song title, cleaned |
| `year` | int or null | Release year if Genius provides it |
| `genius_id` | int | Genius song ID — useful later for lyrics fetching |
| `source` | string | Always `"genius_discography"` |
| `source_rank` | float | Always `1.0` (no chart ranking — these are catalog dumps) |
| `tier` | string | Always `"favorites"` |

The `genius_id` column is NEW for this tier — it's a direct handle for lyrics fetching later in Week 1 Day 6-7. Saves a re-search step.

## Cleaning rules

Re-use `src/corpus/cleaning.py` with all existing fixes (band allowlist including "Tyler, the Creator", proper `featuring` handling, etc.).

Tier-6 specific:
- **Drop demos, alternate versions, live versions** if studio original is present — same rule as before
- **Drop "Remastered" versions** if non-remastered version is present (e.g. Depeche Mode has many remastered editions of older tracks)
- **Drop foreign-language versions** if same song exists in artist's primary language — e.g. Rammstein has English versions of some songs (release as "USA edits"); keep the German original, drop the English version since the artist's primary language is German
- **Drop instrumental versions** if vocal version is present
- **Skip "Genius commentary" or "Producer credits" entries** — Genius sometimes returns these as if they were songs

## Deduplication

**Within Tier 6:** Each (artist, title) combo should appear once per artist. A song co-released by Lil Peep and Lil Uzi Vert appears under one of them — pick whoever is the primary artist on Genius.

**Across Tiers (vs Tiers 1, 2, 3, 4, 5):** After building Tier 6, dedup against all five prior tiers.
- Some Lil Uzi Vert tracks (XO Tour Llif3) and Depeche Mode tracks (Personal Jesus) will be in Tier 1/2/3 already
- Some Lil Peep tracks may be in Tier 5 (TikTok viral)
- Polish artists are unlikely to have prior-tier overlap

Print stats:
- Per-artist count from Genius (raw)
- Per-artist count after Tier 6 internal cleaning
- Per-artist count after cross-tier dedup
- Total final count

## Success criteria

- `data/tier_6_favorites.csv` exists
- Row count between 700 and 1,200
- Each of the 8 artists has at least 30 rows (if any artist falls below 30, log clearly which one)
- No exact duplicates on (artist, title)
- No song appears in Tiers 1-5 AND Tier 6 (modulo normalization)
- Every row has a non-null `genius_id` (used for lyrics fetching later)
- Spot-check: 5 random songs per artist are real, recognizable to fans of that artist (or at least real Genius pages)

## Implementation notes

- Code lives in `src/corpus/build_tier_6.py`
- Re-use `src/corpus/cleaning.py`
- Genius API key is in `.env` as `GENIUS_API_TOKEN`
- Rate limit: ~30 req/min on the free Genius API tier. With 8 artists × ~150 songs each ≈ 1,200 song lookups plus 8 artist searches, total ~50 API calls. Well under any limits — should complete in 2-3 minutes.
- Use `httpx` (already a project dependency)
- Build per-artist helper: `_fetch_artist_discography(artist_name, client) -> list[dict]`
- Print summary at end of run:
  - Per-artist raw count from Genius
  - Per-artist after within-tier cleaning
  - Per-artist after cross-tier dedup (and which tier removed each)
  - Total final count
  - Coverage notes: any artist with <50 final rows should be flagged with possible reasons (Polish coverage thin, etc.)

## Known risks

- **Polish artist coverage** — Genius doesn't index Polish rap as thoroughly as English. Coverage may be 60-80% for Sentino/Malik Montana/Gang Albanii. Accept partial and proceed.
- **Genius rate limits** — should be fine for this volume, but if hit, sleep and retry
- **Genius "featured artist" attribution** — sometimes Genius lists a song under the wrong primary artist. Filter logic should check that the queried artist actually appears as primary, not just featured.
- **Crystal Castles vocal-style** — lyrics on Genius may be sparse or contested for some tracks (Alice Glass screamed/distorted vocals). That's a *lyrics* problem for later, not a Tier 6 problem; we just need the song list.
- **Rammstein song count inflation** — Rammstein has many album versions, live versions, "Live aus Berlin" tracks. Aggressive filter for live/remix is important here.