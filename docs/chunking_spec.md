# Substep 2a: Lyric Chunking Spec

Splits each song's lyrics into overlapping passages, stored in the `passages` table. These passages become the searchable units for retrieval — embeddings will be computed in 2b.

## Goal

For each of the 8,913 songs with non-null `lyrics`:
1. Split into passages of 8 lines with 2-line overlap
2. Prefer not to cross verse boundaries (empty-line breaks)
3. Drop passages shorter than 50 chars after cleanup
4. Store each passage in the `passages` table

Expected output: ~40,000-65,000 passage rows.

## Chunking algorithm

```
For each song:
  lines = lyrics.split('\n')
  # Track verse boundaries: indices where a blank line appears
  verse_boundaries = indices where lines[i].strip() == ''
  
  i = 0
  while i < len(lines):
    # Take next 8 non-blank lines (or fewer if remaining < 8)
    passage_lines = take next 8 lines starting at i
    
    # Prefer to end on a verse boundary within the window
    # (i.e., if there's an empty line in positions 5-8, stop there)
    nearest_boundary = find_blank_line_in_range(i+5, i+8)
    if nearest_boundary:
      passage_lines = lines[i:nearest_boundary]
    else:
      passage_lines = lines[i:i+8]
    
    # Strip blank lines from passage_lines
    passage_text = '\n'.join(line for line in passage_lines if line.strip())
    
    if len(passage_text) >= 50:
      insert into passages: (song_id, passage_text, start_line=i, end_line=i+len(passage_lines))
    
    # Advance by 6 lines (8 - 2 overlap)
    i += 6
```

## Key details

- **Blank lines**: ignored within passages, but their positions inform verse-boundary preference
- **Start/end line numbers**: stored relative to original lyrics (counting all lines including blanks), useful for displaying "passage location in song" later
- **Min passage length**: 50 chars after blank-line removal. Below this is too short to be useful (e.g. "Yeah\nUh\nLet's go").
- **Last passage handling**: if final passage would be < 4 lines, merge into previous passage instead of creating tiny standalone

## Schema (already exists in `passages` table)

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL | auto |
| `song_id` | INTEGER | FK to songs(id), CASCADE on delete |
| `passage_text` | TEXT | the 8-line (or fewer) chunk |
| `start_line` | INTEGER | line index in original lyrics where passage starts |
| `end_line` | INTEGER | line index where passage ends (exclusive) |
| `embedding` | vector(768) | NULL until 2b populates |
| `created_at` | TIMESTAMP | auto |

## Implementation

- Script at `src/embeddings/chunk_lyrics.py`
- Read all songs with `lyrics IS NOT NULL` from DB
- For each song, generate passages, insert in batch
- Use `executemany` for batch inserts
- Make idempotent: `DELETE FROM passages WHERE song_id IN (subset)` before inserting (so re-running gives clean output)
- Or use a different approach: only delete passages from songs being processed. Don't wipe all.
- Best approach: TRUNCATE passages at start of run if `--reset` flag passed; otherwise skip songs that already have passages

## Resumability

Two modes:
- **Default**: skip songs that already have passages (efficient for re-runs after partial failures)
- **`--reset`**: delete all passages first, regenerate everything (use when chunking algorithm changes)

## Output stats

Print:
- Songs processed
- Total passages generated
- Passages per song: avg, min, max
- Passage length in chars: avg, min, max
- Songs that produced 0 passages (e.g. lyrics too short overall)

## Success criteria

- 40,000 - 65,000 passages total
- No songs with NULL lyrics have passages
- Every passage_text has length >= 50 chars
- Average passages per song: 5-8
- Spot-check 10 random songs: passages look sensible (each contains 4-8 real lyric lines)

## Edge cases to handle

- **Very short songs** (lyrics < 50 chars): skip, no passages generated. Already filtered out by Phase 1 cleanup but defensive check helps.
- **Songs with no blank lines** (just continuous text, no verse breaks): fall back to strict 8-line chunks without boundary preference.
- **Songs with excessive blank lines** (poorly formatted): collapse 3+ consecutive blanks to single blank first.
- **Unicode handling**: lyrics contain emojis, accents, non-Latin scripts (Polish, German). Don't break on character indexing — use proper string handling.

## Why this spec

Spec chose 8-line chunks per user decision. 2-line overlap means consecutive passages share 25% of content, reducing the risk that a key phrase falls right on a boundary. Verse-boundary preference helps keep passages musically coherent.