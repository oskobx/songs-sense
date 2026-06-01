# Lyrics Ingestion Spec

Phase 1 of songs-sense. Fills the `lyrics` and `lyrics_source` columns in the `songs` Postgres table for the 9,381 rows already loaded from the seed list.

## Goal

For each song in the `songs` table, fetch full lyrics from the cheapest available source and store them. Expected coverage: 85-90% (~7,500-8,000 songs with lyrics). Skip remainder.

Cost: $0. No paid API tier needed.

## Waterfall

Per song, try sources in order. First success wins. Mark as failed only if all fail.

### Source 1: LRClib live API
- Endpoint: `https://lrclib.net/api/search?artist_name={artist}&track_name={title}`
- Free, no key
- Expected coverage: 50-60% of seed list
- Quality: high — community-maintained
- Speed: ~0.5-1 sec per request (rate-limit politeness)
- For matches with multiple results, pick the one whose duration most closely matches (or just take first result if no duration info)
- Extract `plainLyrics` (preferred) or strip timestamps from `syncedLyrics` if `plainLyrics` is null

### Source 2: theelderemo/genius-lyrics-cleaned (HuggingFace)
- Download full dataset once (2.6 GB)
- Build in-memory dict keyed by `normalize_for_dedup(artist) + "|||" + normalize_for_dedup(title)`
- For each remaining song, look up the key, return lyrics if found
- Expected additional coverage: 20-30% (catching songs LRClib missed)
- Speed: instant once index is built
- No rate limit (local file)

### Source 3: lyricsgenius library
- Use the `lyricsgenius` Python library
- Requires `GENIUS_API_TOKEN` (already in `.env`)
- Direct page scrape, no proxy
- Rate-limit: sleep 3-5 seconds between requests
- Expected additional coverage: 5-10% (mostly newer/niche songs)
- Failures expected: ~10% of requests hit Cloudflare; skip and move on
- Speed: ~3-5 sec per request, ~3-4 hours for the remaining ~2000-3000 songs

### Failure handling
- If all three sources fail, leave `lyrics` and `lyrics_source` as NULL in DB
- Also log to `data/lyrics_failures.csv` with: `artist, title, tier, year, reason`
- Don't retry within the same run; retry by re-running the script (which is idempotent and only touches NULL-lyrics rows)

## Resumability

The script MUST be safely re-runnable. Crashes, kills, network failures should not require redoing work.

Approach:
- Each pass (LRClib / HF / lyricsgenius) queries `SELECT id, artist, title FROM songs WHERE lyrics IS NULL`
- For each found row, fetch lyrics, then `UPDATE songs SET lyrics = ..., lyrics_source = ... WHERE id = ...`
- Re-running picks up where it left off automatically
- No external state file needed

## Lyrics cleaning

Before storing, apply minimal cleaning:
- Strip leading/trailing whitespace
- Remove section headers in square brackets like `[Verse 1]`, `[Chorus]`, `[Bridge]` — these aren't part of the lyrics
- Remove contributor credits like `123 ContributorsLyrics` (Genius scrape artifact)
- Collapse 3+ consecutive newlines into 2 (preserve verse breaks but normalize spacing)
- Keep all the actual lyric text, including features/ad-libs

Do NOT:
- Remove parenthetical ad-libs (these are part of lyrics)
- Translate or transliterate
- Lowercase or normalize punctuation

## Output schema

Updates the existing `songs` table:
- `lyrics` column: full plain text lyrics, multiline, UTF-8
- `lyrics_source` column: one of `'lrclib'`, `'huggingface'`, `'lyricsgenius'`

Also writes `data/lyrics_failures.csv` for songs that all three sources failed on.

## Success criteria

- ≥80% of songs in the `songs` table have non-null `lyrics` (~7,500 songs)
- `lyrics_source` is populated for every song with lyrics
- Per-source breakdown printed
- Per-tier coverage printed (some tiers will be lower — Polish artists in Tier 6 especially)
- `data/lyrics_failures.csv` exists with the failures
- Average lyrics length is sensible (200-2000 chars; outliers worth spot-checking)

## Implementation

- Three scripts in `src/lyrics/`:
  - `fetch_lrclib.py` — Source 1 pass
  - `fetch_huggingface.py` — Source 2 pass
  - `fetch_lyricsgenius.py` — Source 3 pass
- Plus `src/lyrics/clean_lyrics.py` with shared cleaning function
- Plus `src/lyrics/ingest.py` as the orchestrator: runs all three in sequence, prints stats between each
- Each fetch script can be run standalone (useful for debugging one source)

Dependencies to add:
- `httpx` (already added)
- `lyricsgenius` (`uv add lyricsgenius`)
- `datasets` (already added from eval step)

## Per-source implementation notes

### LRClib
- Use httpx async for concurrency — can safely do 5-10 concurrent requests
- Endpoint returns JSON list; pick best match
- Handle 404 gracefully (no results)
- Sleep 100ms between requests to be polite
- Expected runtime: ~30-60 min for 9,000 songs with async

### HuggingFace
- Download full dataset using `datasets.load_dataset()` — first run will cache ~2.6 GB
- Build in-memory dict mapping `normalized_key → lyrics` (RAM: ~1-2 GB)
- For each NULL-lyrics song, lookup key, return if found
- Expected runtime: ~5 min download + ~10 sec lookup pass

### lyricsgenius
- Use `lyricsgenius.Genius(GENIUS_API_TOKEN)` with `timeout=10`, `retries=2`
- Set `remove_section_headers=True` to avoid `[Verse]` markers
- Set `skip_non_songs=True`
- Sleep 3 seconds between requests
- Catch and log: `Timeout`, `ConnectionError`, `RuntimeError` (Genius lib raises these on Cloudflare blocks)
- Expected runtime: ~3-4 hours for remaining ~2000-3000 songs

## Known risks

- **Cloudflare blocking lyricsgenius**: if your IP gets blocked, the lyricsgenius pass will fail at high rate. Mitigation: longer sleeps (5+ sec), accept lower coverage, or pause and retry from a different network.
- **LRClib API downtime**: it's a community service. If down, skip LRClib pass and proceed to HF.
- **HuggingFace dataset taken down**: same risk that hit our eval. If theelderemo is gone, sebastiandizon is the backup (lower coverage but stable).
- **Polish/foreign-language coverage**: all three sources skew English. Expect 40-60% coverage for Polish artists vs 90%+ for English. Document the gap; don't try to fix in this phase.