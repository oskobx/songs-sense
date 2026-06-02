"""Source 2: theelderemo/genius-lyrics-cleaned HuggingFace dataset.

Downloads the full dataset once (cached by HuggingFace), builds an in-memory
lookup dict, then fills lyrics for songs still NULL after the LRClib pass.

Run standalone:
    python -m src.lyrics.fetch_huggingface
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.corpus.cleaning import normalize_for_dedup  # noqa: E402
from src.lyrics.clean_lyrics import clean_lyrics  # noqa: E402

load_dotenv()

DATASET_NAME = "theelderemo/genius-lyrics-cleaned"


def build_lookup(dataset_name: str) -> tuple[dict[str, str], int]:
    """Download (or load from cache) the HF dataset and return a lookup dict.

    Key  : normalize_for_dedup(artist) + "|||" + normalize_for_dedup(title)
    Value: cleaned lyrics string

    Returns (lookup_dict, total_rows_in_dataset).
    """
    # Import here so startup is fast if the module is imported but not called
    from datasets import load_dataset  # type: ignore[import-untyped]

    hf_token = os.environ.get("HF_TOKEN") or None

    print(f"Loading dataset {dataset_name!r} (first run downloads ~2.6 GB, then cached)…")
    if hf_token:
        print("  Using HF_TOKEN from .env")
    t0 = time.monotonic()
    ds = load_dataset(dataset_name, split="train", token=hf_token)
    elapsed = time.monotonic() - t0
    total_rows = len(ds)
    print(f"  Loaded {total_rows:,} rows in {elapsed:.1f}s")

    print("Building in-memory lookup dict…")
    t1 = time.monotonic()
    lookup: dict[str, str] = {}
    skipped = 0

    for row in ds:
        artist = str(row.get("artist") or "").strip()
        title = str(row.get("title") or "").strip()
        lyrics_raw = (row.get("lyrics") or row.get("lyrics_clean") or "").strip()

        if not artist or not title or not lyrics_raw:
            skipped += 1
            continue

        key = normalize_for_dedup(artist) + "|||" + normalize_for_dedup(title)
        # Keep first occurrence if duplicate keys exist
        if key not in lookup:
            lookup[key] = lyrics_raw

    elapsed2 = time.monotonic() - t1
    print(f"  Dict size : {len(lookup):,} entries  (skipped {skipped:,} rows with missing fields)")
    print(f"  Built in  : {elapsed2:.1f}s")

    return lookup, total_rows


def print_db_stats(database_url: str) -> None:
    with psycopg.connect(database_url) as conn:
        total = conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
        with_lyrics = conn.execute(
            "SELECT COUNT(*) FROM songs WHERE lyrics IS NOT NULL"
        ).fetchone()[0]
        null_lyrics = conn.execute(
            "SELECT COUNT(*) FROM songs WHERE lyrics IS NULL"
        ).fetchone()[0]

        tier_rows = conn.execute(
            """
            SELECT
                tier,
                COUNT(*) AS total,
                SUM(CASE WHEN lyrics IS NOT NULL THEN 1 ELSE 0 END) AS fetched
            FROM songs
            GROUP BY tier
            ORDER BY tier
            """
        ).fetchall()

        hf_count = conn.execute(
            "SELECT COUNT(*) FROM songs WHERE lyrics_source = 'huggingface'"
        ).fetchone()[0]

    print("\n=== DB coverage after HuggingFace pass ===")
    print(f"  Total songs         : {total:,}")
    print(f"  With lyrics (all)   : {with_lyrics:,}  ({with_lyrics/total*100:.1f}%)")
    print(f"    — from huggingface: {hf_count:,}")
    print(f"  Still NULL          : {null_lyrics:,}  ({null_lyrics/total*100:.1f}%)")

    print("\n  Per-tier total coverage (all sources so far):")
    print(f"  {'Tier':<20}  {'Has lyrics':>10}  {'Total':>7}  {'Cover%':>7}")
    print(f"  {'-'*20}  {'-'*10}  {'-'*7}  {'-'*7}")
    for tier, total_t, fetched_t in tier_rows:
        pct = fetched_t / total_t * 100 if total_t else 0
        print(f"  {tier:<20}  {fetched_t:>10,}  {total_t:>7,}  {pct:>6.1f}%")


def run_huggingface_pass() -> dict[str, int]:
    database_url = os.environ["DATABASE_URL"]

    lookup, _total_rows = build_lookup(DATASET_NAME)

    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT id, artist, title, tier, year FROM songs WHERE lyrics IS NULL ORDER BY id"
        ).fetchall()

    null_count = len(rows)
    print(f"\nSongs still NULL after LRClib: {null_count:,}")

    hits = 0
    misses = 0
    t0 = time.monotonic()

    with psycopg.connect(database_url) as conn:
        for song_id, artist, title, tier, year in rows:
            key = normalize_for_dedup(artist) + "|||" + normalize_for_dedup(title)
            raw_lyrics = lookup.get(key)

            if raw_lyrics:
                cleaned = clean_lyrics(raw_lyrics)
                if cleaned:
                    conn.execute(
                        "UPDATE songs SET lyrics = %s, lyrics_source = 'huggingface' WHERE id = %s",
                        (cleaned, song_id),
                    )
                    hits += 1
                else:
                    misses += 1
            else:
                misses += 1

        conn.commit()

    elapsed = time.monotonic() - t0

    print(f"\n--- HuggingFace pass results ({elapsed:.1f}s) ---")
    print(f"  Songs queried (NULL): {null_count:,}")
    print(f"  Hits saved to DB    : {hits:,}  ({hits/null_count*100:.1f}% of NULL set)")
    print(f"  Misses              : {misses:,}  ({misses/null_count*100:.1f}%)")

    print_db_stats(database_url)
    return {"hits": hits, "misses": misses}


def main() -> None:
    run_huggingface_pass()


if __name__ == "__main__":
    main()
