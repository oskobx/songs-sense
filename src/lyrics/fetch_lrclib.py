"""Source 1: LRClib live API lyrics fetcher.

Fetches lyrics for all songs WHERE lyrics IS NULL (resumable).
Run standalone:
    python -m src.lyrics.fetch_lrclib            # full pass
    python -m src.lyrics.fetch_lrclib --limit 100 # sample pass
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys
import time
from pathlib import Path

import httpx
import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.lyrics.clean_lyrics import clean_lyrics  # noqa: E402

load_dotenv()

LRCLIB_SEARCH = "https://lrclib.net/api/search"
CONCURRENCY = 5
POLITE_SLEEP = 0.15  # seconds between requests
FAILURES_CSV = ROOT / "data" / "lyrics_failures.csv"

# Strips LRC timestamp tags: [mm:ss.xx] or [mm:ss:xx]
_LRC_TS_RE = re.compile(r"^\[\d{2}:\d{2}[.:]\d{2}\]\s?", re.MULTILINE)


def strip_lrc_timestamps(synced: str) -> str:
    return _LRC_TS_RE.sub("", synced).strip()


async def fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    song_id: int,
    artist: str,
    title: str,
) -> tuple[int, str | None, str | None]:
    """Return (song_id, lyrics_text, error_reason). lyrics_text is None on failure."""
    async with sem:
        try:
            resp = await client.get(
                LRCLIB_SEARCH,
                params={"artist_name": artist, "track_name": title},
                timeout=15,
            )
            await asyncio.sleep(POLITE_SLEEP)

            if resp.status_code in (404, 204):
                return song_id, None, "no_results"

            resp.raise_for_status()
            results = resp.json()

            if not results:
                return song_id, None, "no_results"

            # Prefer entry with plainLyrics; otherwise take first
            chosen = next((r for r in results if r.get("plainLyrics")), results[0])

            plain = chosen.get("plainLyrics") or ""
            synced = chosen.get("syncedLyrics") or ""

            if plain:
                text = plain
            elif synced:
                text = strip_lrc_timestamps(synced)
            else:
                return song_id, None, "no_lyrics_content"

            cleaned = clean_lyrics(text)
            if not cleaned:
                return song_id, None, "empty_after_clean"

            return song_id, cleaned, None

        except httpx.TimeoutException:
            return song_id, None, "timeout"
        except httpx.HTTPStatusError as exc:
            return song_id, None, f"http_{exc.response.status_code}"
        except Exception as exc:
            return song_id, None, f"error:{type(exc).__name__}"


async def run_lrclib_pass(limit: int | None = None) -> dict[str, int]:
    """Fetch lyrics for NULL-lyrics songs from LRClib. Returns per-outcome counts."""
    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(database_url) as conn:
        query = "SELECT id, artist, title, tier, year FROM songs WHERE lyrics IS NULL ORDER BY id"
        if limit is not None:
            query += f" LIMIT {limit}"
        rows = conn.execute(query).fetchall()

    total = len(rows)
    print(f"Songs with NULL lyrics to process: {total}")
    if total == 0:
        print("Nothing to do.")
        return {"success": 0, "no_results": 0, "error": 0}

    sem = asyncio.Semaphore(CONCURRENCY)
    start = time.monotonic()

    async with httpx.AsyncClient(
        headers={"User-Agent": "songs-sense/0.1 (portfolio project, contact: oskobx@gmail.com)"}
    ) as client:
        tasks = [
            fetch_one(client, sem, row[0], row[1], row[2])
            for row in rows
        ]
        results = await asyncio.gather(*tasks)

    elapsed = time.monotonic() - start

    # Pair results with original row metadata
    row_by_id = {row[0]: row for row in rows}
    counts: dict[str, int] = {"success": 0, "no_results": 0, "error": 0}
    failures: list[dict[str, str]] = []
    sample_lyrics: list[tuple[str, str, str]] = []

    with psycopg.connect(database_url) as conn:
        for song_id, lyrics, reason in results:
            _, artist, title, tier, year = row_by_id[song_id]

            if lyrics:
                conn.execute(
                    "UPDATE songs SET lyrics = %s, lyrics_source = 'lrclib' WHERE id = %s",
                    (lyrics, song_id),
                )
                counts["success"] += 1
                if len(sample_lyrics) < 5:
                    sample_lyrics.append((artist, title, lyrics[:200]))
            else:
                bucket = "no_results" if reason == "no_results" else "error"
                counts[bucket] += 1
                failures.append(
                    {
                        "artist": artist,
                        "title": title,
                        "tier": tier or "",
                        "year": str(year or ""),
                        "reason": reason or "unknown",
                    }
                )
        conn.commit()

    # Append failures to CSV
    if failures:
        file_exists = FAILURES_CSV.exists()
        with FAILURES_CSV.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["artist", "title", "tier", "year", "reason"])
            if not file_exists:
                writer.writeheader()
            writer.writerows(failures)

    print(f"\n--- LRClib pass results ({elapsed:.1f}s) ---")
    print(f"  Songs processed : {total}")
    print(f"  Success         : {counts['success']}  ({counts['success']/total*100:.1f}%)")
    print(f"  No results      : {counts['no_results']}  ({counts['no_results']/total*100:.1f}%)")
    print(f"  Errors          : {counts['error']}  ({counts['error']/total*100:.1f}%)")
    if failures:
        print(f"  Failures logged : {len(failures)} → {FAILURES_CSV}")

    if sample_lyrics:
        print("\n--- 5 sample lyrics fetched ---")
        for artist, title, snippet in sample_lyrics:
            print(f"\n  [{artist} — {title}]")
            print(f"  {snippet!r}")

    return counts


def print_db_stats(database_url: str) -> None:
    """Query the DB and print per-tier coverage + average lyrics length."""
    with psycopg.connect(database_url) as conn:
        total = conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
        with_lyrics = conn.execute(
            "SELECT COUNT(*) FROM songs WHERE lyrics IS NOT NULL AND lyrics_source = 'lrclib'"
        ).fetchone()[0]
        null_lyrics = conn.execute(
            "SELECT COUNT(*) FROM songs WHERE lyrics IS NULL"
        ).fetchone()[0]

        avg_len = conn.execute(
            "SELECT AVG(LENGTH(lyrics)) FROM songs WHERE lyrics IS NOT NULL AND lyrics_source = 'lrclib'"
        ).fetchone()[0]

        tier_rows = conn.execute(
            """
            SELECT
                tier,
                COUNT(*) AS total,
                SUM(CASE WHEN lyrics IS NOT NULL AND lyrics_source = 'lrclib' THEN 1 ELSE 0 END) AS fetched
            FROM songs
            GROUP BY tier
            ORDER BY tier
            """
        ).fetchall()

        rate_limit_rows = conn.execute(
            "SELECT COUNT(*) FROM songs WHERE lyrics IS NULL"
        ).fetchone()[0]

    print("\n=== LRClib full-pass DB stats ===")
    print(f"  Total songs in DB       : {total:,}")
    print(f"  Saved to DB (lrclib)    : {with_lyrics:,}  ({with_lyrics/total*100:.1f}%)")
    print(f"  Still NULL (all sources): {null_lyrics:,}  ({null_lyrics/total*100:.1f}%)")
    if avg_len:
        print(f"  Avg lyrics length       : {avg_len:.0f} chars")

    print("\n  Per-tier coverage (lrclib only):")
    print(f"  {'Tier':<20}  {'Fetched':>7}  {'Total':>7}  {'Cover%':>7}")
    print(f"  {'-'*20}  {'-'*7}  {'-'*7}  {'-'*7}")
    for tier, total_t, fetched_t in tier_rows:
        pct = fetched_t / total_t * 100 if total_t else 0
        print(f"  {tier:<20}  {fetched_t:>7,}  {total_t:>7,}  {pct:>6.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch lyrics from LRClib")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only first N NULL-lyrics songs (for testing)"
    )
    args = parser.parse_args()
    asyncio.run(run_lrclib_pass(limit=args.limit))
    database_url = os.environ["DATABASE_URL"]
    print_db_stats(database_url)


if __name__ == "__main__":
    main()
