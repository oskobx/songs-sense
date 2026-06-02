"""Substep 2a: Split lyrics into overlapping passages and store in DB.

Usage:
    python -m src.embeddings.chunk_lyrics              # skip songs with existing passages
    python -m src.embeddings.chunk_lyrics --reset      # TRUNCATE passages then regenerate
    python -m src.embeddings.chunk_lyrics --preview 10 # print 10 random songs, no DB write
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

load_dotenv()

# ---------------------------------------------------------------------------
# Chunking parameters
# ---------------------------------------------------------------------------
CHUNK_LINES = 8
OVERLAP_LINES = 2
STEP = CHUNK_LINES - OVERLAP_LINES  # 6
MIN_PASSAGE_CHARS = 50
MIN_LAST_PASSAGE_LINES = 4  # merge into prev if final passage is shorter

_EXCESS_NEWLINES_RE = re.compile(r"\n{3,}")


# ---------------------------------------------------------------------------
# Pure chunking logic
# ---------------------------------------------------------------------------

def chunk_lyrics(song_id: int, lyrics: str) -> list[dict]:
    """Return a list of passage dicts for the given song lyrics.

    Each dict has: song_id, passage_text, start_line, end_line.
    Does NOT touch the database.
    """
    # Collapse 3+ consecutive newlines to 2 (defensive — clean_lyrics should do this)
    lyrics = _EXCESS_NEWLINES_RE.sub("\n\n", lyrics)
    all_lines = lyrics.split("\n")

    raw: list[tuple[int, int]] = []  # (start_line, end_line) pairs
    i = 0

    while i < len(all_lines):
        default_end = min(i + CHUNK_LINES, len(all_lines))

        # Prefer ending on a verse boundary (blank line) in [i+5, i+8)
        end = default_end
        for j in range(i + (STEP - 1), min(i + CHUNK_LINES, len(all_lines))):
            if not all_lines[j].strip():
                end = j  # stop before the blank line
                break

        raw.append((i, end))
        i += STEP

    # Last-passage handling: merge into previous if < MIN_LAST_PASSAGE_LINES non-blank lines
    if len(raw) >= 2:
        last_start, last_end = raw[-1]
        last_non_blank = sum(1 for l in all_lines[last_start:last_end] if l.strip())
        if last_non_blank < MIN_LAST_PASSAGE_LINES:
            prev_start, _ = raw[-2]
            raw[-2] = (prev_start, last_end)
            raw.pop()

    # Build passage dicts, applying min-length filter
    passages: list[dict] = []
    for start, end in raw:
        text = "\n".join(l for l in all_lines[start:end] if l.strip())
        if len(text) >= MIN_PASSAGE_CHARS:
            passages.append(
                {
                    "song_id": song_id,
                    "passage_text": text,
                    "start_line": start,
                    "end_line": end,
                }
            )

    return passages


# ---------------------------------------------------------------------------
# Preview mode
# ---------------------------------------------------------------------------

def preview(n: int) -> None:
    url = os.environ["DATABASE_URL"]
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT id, artist, title, lyrics FROM songs WHERE lyrics IS NOT NULL"
        ).fetchall()

    sample = random.sample(rows, min(n, len(rows)))

    for song_id, artist, title, lyrics in sample:
        passages = chunk_lyrics(song_id, lyrics)
        print(f"\n{'='*70}")
        print(f"  {artist} — {title}  (id={song_id})")
        print(f"  {len(passages)} passages from {len(lyrics.split(chr(10)))} lines")
        print(f"{'='*70}")
        for idx, p in enumerate(passages, 1):
            lines_in_passage = p["passage_text"].count("\n") + 1
            print(
                f"\n  [Passage {idx}  lines {p['start_line']}–{p['end_line']}  "
                f"{lines_in_passage} lines  {len(p['passage_text'])} chars]"
            )
            for line in p["passage_text"].split("\n"):
                print(f"    {line}")


# ---------------------------------------------------------------------------
# DB insert
# ---------------------------------------------------------------------------

INSERT_SQL = """
INSERT INTO passages (song_id, passage_text, start_line, end_line)
VALUES (%s, %s, %s, %s)
"""


def run(reset: bool) -> None:
    url = os.environ["DATABASE_URL"]
    with psycopg.connect(url) as conn:
        if reset:
            conn.execute("TRUNCATE TABLE passages RESTART IDENTITY")
            conn.commit()
            print("passages table truncated.")
            songs_to_process = conn.execute(
                "SELECT id, artist, title, lyrics FROM songs WHERE lyrics IS NOT NULL"
            ).fetchall()
        else:
            # Skip songs that already have passages
            songs_to_process = conn.execute(
                """
                SELECT s.id, s.artist, s.title, s.lyrics
                FROM songs s
                WHERE s.lyrics IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM passages p WHERE p.song_id = s.id
                  )
                """
            ).fetchall()

        total_songs = len(songs_to_process)
        print(f"Songs to process: {total_songs}")

        all_passage_counts: list[int] = []
        all_passage_lengths: list[int] = []
        zero_passage_songs: list[str] = []

        batch: list[tuple] = []
        BATCH_SIZE = 500

        def flush(conn: psycopg.Connection) -> None:
            if batch:
                with conn.cursor() as cur:
                    cur.executemany(INSERT_SQL, batch)
                conn.commit()
                batch.clear()

        for i, (song_id, artist, title, lyrics) in enumerate(songs_to_process, 1):
            passages = chunk_lyrics(song_id, lyrics)

            count = len(passages)
            all_passage_counts.append(count)
            if count == 0:
                zero_passage_songs.append(f"{artist} — {title}")

            for p in passages:
                all_passage_lengths.append(len(p["passage_text"]))
                batch.append(
                    (p["song_id"], p["passage_text"], p["start_line"], p["end_line"])
                )

            if len(batch) >= BATCH_SIZE:
                flush(conn)

            if i % 500 == 0 or i == total_songs:
                print(f"  {i}/{total_songs} songs processed …")

        flush(conn)

    # ---------------------------------------------------------------------------
    # Stats
    # ---------------------------------------------------------------------------
    total_passages = sum(all_passage_counts)
    print(f"\n{'='*50}")
    print(f"Songs processed:          {total_songs}")
    print(f"Total passages generated: {total_passages}")

    if all_passage_counts:
        avg_p = total_passages / len(all_passage_counts)
        print(f"Passages per song:        avg={avg_p:.1f}  min={min(all_passage_counts)}  max={max(all_passage_counts)}")

    if all_passage_lengths:
        avg_l = sum(all_passage_lengths) / len(all_passage_lengths)
        print(f"Passage length (chars):   avg={avg_l:.0f}  min={min(all_passage_lengths)}  max={max(all_passage_lengths)}")

    print(f"Songs with 0 passages:    {len(zero_passage_songs)}")
    if zero_passage_songs:
        for name in zero_passage_songs[:20]:
            print(f"  {name}")
        if len(zero_passage_songs) > 20:
            print(f"  … and {len(zero_passage_songs) - 20} more")
    print(f"{'='*50}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Chunk lyrics into passages.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--reset",
        action="store_true",
        help="TRUNCATE passages table then regenerate everything",
    )
    group.add_argument(
        "--preview",
        metavar="N",
        type=int,
        help="Print N random songs' passages without touching DB",
    )
    args = parser.parse_args()

    if args.preview:
        preview(args.preview)
    else:
        run(reset=args.reset)


if __name__ == "__main__":
    main()
