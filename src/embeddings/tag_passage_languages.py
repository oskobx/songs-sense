"""Tag each passage with its detected language using lingua-py.

Usage:
    python -m src.embeddings.tag_passage_languages              # test 100, prompt to continue
    python -m src.embeddings.tag_passage_languages --skip-test  # full run, no prompt

Stores ISO 639-1 codes ('en', 'pl', 'de', ...) or 'unknown' in passages.language.
Resumable via WHERE language IS NULL.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from lingua import Language, LanguageDetectorBuilder

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

load_dotenv()

DB_BATCH = 1000

# Languages to detect (passage tagging uses a wider set than query routing)
DETECT_LANGUAGES = [
    Language.ENGLISH,
    Language.POLISH,
    Language.GERMAN,
    Language.SPANISH,
    Language.FRENCH,
    Language.ITALIAN,
    Language.PORTUGUESE,
    Language.KOREAN,
    Language.JAPANESE,
]

# Explicit ISO mapping — lingua Language.name is e.g. "POLISH", not "pl"
LANG_TO_ISO: dict[Language, str] = {
    Language.ENGLISH: "en",
    Language.POLISH: "pl",
    Language.GERMAN: "de",
    Language.SPANISH: "es",
    Language.FRENCH: "fr",
    Language.ITALIAN: "it",
    Language.PORTUGUESE: "pt",
    Language.KOREAN: "ko",
    Language.JAPANESE: "ja",
}

detector = LanguageDetectorBuilder.from_languages(*DETECT_LANGUAGES).build()


def detect_language(text: str) -> str:
    lang = detector.detect_language_of(text)
    return LANG_TO_ISO.get(lang, "unknown")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def fetch_null_batch(conn: psycopg.Connection, limit: int) -> list[tuple[int, str]]:
    return conn.execute(
        "SELECT id, passage_text FROM passages WHERE language IS NULL LIMIT %s",
        (limit,),
    ).fetchall()


def write_languages(
    conn: psycopg.Connection,
    id_lang_pairs: list[tuple[int, str]],
) -> None:
    with conn.pipeline():
        for pid, lang in id_lang_pairs:
            conn.execute(
                "UPDATE passages SET language = %s WHERE id = %s",
                (lang, pid),
            )
    conn.commit()


def count_null(conn: psycopg.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM passages WHERE language IS NULL"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Test run (first 100 passages)
# ---------------------------------------------------------------------------

def run_test(conn: psycopg.Connection) -> float:
    print("\n--- Test run: first 100 passages ---")
    rows = conn.execute(
        "SELECT id, passage_text FROM passages WHERE language IS NULL LIMIT 100"
    ).fetchall()
    if not rows:
        print("No NULL language rows — already fully tagged.")
        return 0.0

    t0 = time.perf_counter()
    pairs = [(r[0], detect_language(r[1])) for r in rows]
    elapsed = time.perf_counter() - t0

    throughput = len(rows) / elapsed
    counts = Counter(lang for _, lang in pairs)

    print(f"  Passages tagged   : {len(rows)}")
    print(f"  Total time        : {elapsed:.2f}s")
    print(f"  Throughput        : {throughput:.0f} passages/sec")
    print(f"  Language breakdown:")
    for lang, n in counts.most_common():
        print(f"    {lang:>8s}  {n:>3d}  ({n/len(rows)*100:.1f}%)")

    return throughput


def estimate_runtime(throughput: float, null_count: int) -> None:
    if throughput <= 0:
        return
    eta = null_count / throughput
    print(f"\n  Null passages remaining : {null_count:,}")
    print(f"  Estimated runtime       : {eta:.0f}s  ({eta/60:.1f} min)")


# ---------------------------------------------------------------------------
# Full tagging pass
# ---------------------------------------------------------------------------

def tag_all_passages(conn: psycopg.Connection) -> None:
    total_start = time.perf_counter()
    processed = 0
    lang_totals: Counter = Counter()

    while True:
        rows = fetch_null_batch(conn, DB_BATCH)
        if not rows:
            break

        pairs = [(r[0], detect_language(r[1])) for r in rows]
        write_languages(conn, pairs)

        processed += len(rows)
        for _, lang in pairs:
            lang_totals[lang] += 1

        elapsed = time.perf_counter() - total_start
        throughput = processed / elapsed
        remaining = count_null(conn)
        eta = remaining / throughput if throughput > 0 else 0
        print(
            f"  Tagged {processed:>6,} | remaining {remaining:>6,} | "
            f"{throughput:.0f} p/s | ETA {eta:.0f}s"
        )

    total_elapsed = time.perf_counter() - total_start
    total = sum(lang_totals.values())

    print(f"\n  Tagging complete: {processed:,} passages in {total_elapsed:.1f}s "
          f"({processed/total_elapsed:.0f} p/s)")
    print("\n  Final language breakdown:")
    for lang, n in lang_totals.most_common():
        print(f"    {lang:>8s}  {n:>7,}  ({n/total*100:.1f}%)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Tag passages with language codes")
    parser.add_argument("--skip-test", action="store_true",
                        help="Skip test run and go straight to full pass")
    args = parser.parse_args()

    url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(url)

    print(f"Detector languages: {[LANG_TO_ISO[l] for l in DETECT_LANGUAGES]}")

    if not args.skip_test:
        null_before = count_null(conn)
        throughput = run_test(conn)
        estimate_runtime(throughput, null_before)

        answer = input("\nContinue with full tagging pass? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted. Re-run with --skip-test to bypass this prompt.")
            conn.close()
            return

    tag_all_passages(conn)
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
