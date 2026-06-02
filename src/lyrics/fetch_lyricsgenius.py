"""Source 3: lyricsgenius library (Genius API scrape).

Fetches lyrics for songs still NULL after LRClib + HuggingFace passes.
Rate-limited to 3s between requests; catches Cloudflare blocks gracefully.

Run standalone:
    python -m src.lyrics.fetch_lyricsgenius            # full pass
    python -m src.lyrics.fetch_lyricsgenius --limit 50 # sample pass
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.lyrics.clean_lyrics import clean_lyrics  # noqa: E402

load_dotenv()

SLEEP_BETWEEN = 3  # seconds between requests
FAILURES_CSV = ROOT / "data" / "lyrics_failures.csv"


def make_genius_client():
    import lyricsgenius  # type: ignore[import-untyped]

    token = os.environ.get("GENIUS_TOKEN") or ""
    if not token:
        raise RuntimeError("GENIUS_TOKEN not set in .env")

    genius = lyricsgenius.Genius(
        token,
        timeout=10,
        retries=2,
        remove_section_headers=True,
        skip_non_songs=True,
    )
    genius.excluded_terms = ["(Remix)", "(Live)"]
    return genius


def _strip_slash_title(title: str) -> str:
    """'Maggie May/Reason To Believe' → 'Maggie May'. Genius can't handle double-A-sides."""
    return title.split("/")[0].strip()


def fetch_one(genius, artist: str, title: str) -> tuple[str | None, str | None]:
    """Return (cleaned_lyrics, error_reason). lyrics is None on failure."""
    search_title = _strip_slash_title(title)
    try:
        song = genius.search_song(search_title, artist)
        if song is None or not song.lyrics:
            return None, "no_results"
        cleaned = clean_lyrics(song.lyrics)
        if not cleaned:
            return None, "empty_after_clean"
        return cleaned, None
    except TimeoutError:
        return None, "timeout"
    except ConnectionError:
        return None, "connection_error"
    except RuntimeError as exc:
        # lyricsgenius raises RuntimeError on Cloudflare blocks
        return None, f"runtime:{str(exc)[:60]}"
    except Exception as exc:
        return None, f"error:{type(exc).__name__}"


def run_lyricsgenius_pass(limit: int | None = None) -> dict[str, int]:
    """Fetch lyrics for NULL-lyrics songs via lyricsgenius. Returns per-outcome counts."""
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

    genius = make_genius_client()
    counts: dict[str, int] = {"success": 0, "no_results": 0, "error": 0}
    failures: list[dict[str, str]] = []
    sample_lyrics: list[tuple[str, str, str]] = []
    start = time.monotonic()

    for i, (song_id, artist, title, tier, year) in enumerate(rows):
        if i > 0:
            time.sleep(SLEEP_BETWEEN)

        lyrics, reason = fetch_one(genius, artist, title)

        if lyrics:
            with psycopg.connect(database_url) as conn:
                conn.execute(
                    "UPDATE songs SET lyrics = %s, lyrics_source = 'lyricsgenius' WHERE id = %s",
                    (lyrics, song_id),
                )
                conn.commit()
            counts["success"] += 1
            if len(sample_lyrics) < 3:
                sample_lyrics.append((artist, title, lyrics[:200]))
        else:
            bucket = "no_results" if reason == "no_results" else "error"
            counts[bucket] += 1
            failures.append({
                "artist": artist,
                "title": title,
                "tier": tier or "",
                "year": str(year or ""),
                "reason": reason or "unknown",
            })

        # Progress every 10 songs
        if (i + 1) % 10 == 0:
            elapsed = time.monotonic() - start
            rate = (i + 1) / elapsed
            remaining = total - (i + 1)
            eta_min = remaining / rate / 60
            print(
                f"  [{i+1}/{total}] success={counts['success']} "
                f"no_results={counts['no_results']} errors={counts['error']} "
                f"ETA ~{eta_min:.0f}m"
            )

    elapsed = time.monotonic() - start

    # Append failures to CSV
    if failures:
        file_exists = FAILURES_CSV.exists()
        with FAILURES_CSV.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["artist", "title", "tier", "year", "reason"])
            if not file_exists:
                writer.writeheader()
            writer.writerows(failures)

    print(f"\n--- lyricsgenius pass results ({elapsed:.1f}s) ---")
    print(f"  Songs processed : {total}")
    print(f"  Success         : {counts['success']}  ({counts['success']/total*100:.1f}%)")
    print(f"  No results      : {counts['no_results']}  ({counts['no_results']/total*100:.1f}%)")
    print(f"  Errors          : {counts['error']}  ({counts['error']/total*100:.1f}%)")
    if failures:
        print(f"  Failures logged : {len(failures)} → {FAILURES_CSV}")

    if sample_lyrics:
        print("\n--- 3 sample lyrics fetched ---")
        for artist, title, snippet in sample_lyrics:
            print(f"\n  [{artist} — {title}]")
            print(f"  {snippet!r}")

    return counts


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
        lg_count = conn.execute(
            "SELECT COUNT(*) FROM songs WHERE lyrics_source = 'lyricsgenius'"
        ).fetchone()[0]

    print("\n=== DB coverage after lyricsgenius sample ===")
    print(f"  Total songs            : {total:,}")
    print(f"  With lyrics (all)      : {with_lyrics:,}  ({with_lyrics/total*100:.1f}%)")
    print(f"    — from lyricsgenius  : {lg_count:,}")
    print(f"  Still NULL             : {null_lyrics:,}  ({null_lyrics/total*100:.1f}%)")

    print("\n  Per-tier total coverage (all sources so far):")
    print(f"  {'Tier':<20}  {'Has lyrics':>10}  {'Total':>7}  {'Cover%':>7}")
    print(f"  {'-'*20}  {'-'*10}  {'-'*7}  {'-'*7}")
    for tier, total_t, fetched_t in tier_rows:
        pct = fetched_t / total_t * 100 if total_t else 0
        print(f"  {tier:<20}  {fetched_t:>10,}  {total_t:>7,}  {pct:>6.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch lyrics via lyricsgenius")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only first N NULL-lyrics songs (for testing)"
    )
    args = parser.parse_args()
    counts = run_lyricsgenius_pass(limit=args.limit)
    database_url = os.environ["DATABASE_URL"]
    print_db_stats(database_url)

    if args.limit:
        elapsed_per_song = (SLEEP_BETWEEN + 1.5)  # ~3s sleep + ~1.5s API
        remaining_null = 1548 - counts["success"]
        est_min = remaining_null * elapsed_per_song / 60
        print(f"\n  Estimated full-pass time for ~{remaining_null} remaining songs: ~{est_min:.0f} min")


if __name__ == "__main__":
    main()
