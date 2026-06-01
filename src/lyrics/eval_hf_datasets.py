"""Evaluate HuggingFace lyrics dataset coverage against the seed list.

Usage:
    python -m src.lyrics.eval_hf_datasets

Streams the first SAMPLE_SIZE rows from each candidate dataset and reports
what fraction of our 9381-song seed list is covered. No data is written to
Postgres — this is read-only evaluation.

CAVEAT: streaming a subset gives a lower-bound coverage estimate. For accurate
numbers we'd need the full download; use this for initial signal only.

Candidates tested (original 3 from spec were removed from Hub):
  - sebastiandizon/genius-song-lyrics   (Genius, has language detection)
  - Dr3dre/Genius-song-lyrics-cleaned   (cleaned version of above)
  - theelderemo/genius-lyrics-cleaned   (simpler Genius set)
  - ernestchu/lrclib-20250319           (LRClib — synced+plain lyrics, in our stack)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd
from datasets import load_dataset  # type: ignore[import-untyped]

# Make sure src/ is on the path so we can import corpus.cleaning
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.corpus.cleaning import normalize_for_dedup  # noqa: E402

SEED_LIST_PATH = ROOT / "data" / "seed_list.csv"
SAMPLE_SIZE = 50_000


# --- per-dataset schema adapters -------------------------------------------
# Each adapter receives a raw dataset row (dict) and returns
# (artist, title, lyrics_present, language) or None to skip the row.

def _adapt_genius_standard(row: dict[str, Any]) -> tuple[str, str, bool, str | None] | None:
    """Covers sebastiandizon/genius-song-lyrics, Dr3dre/Genius-song-lyrics-cleaned,
    theelderemo/genius-lyrics-cleaned, JakubBilski/rap-vs-country-* — all share
    the columns: artist, title, lyrics, (optional) language."""
    artist = row.get("artist") or ""
    title = row.get("title") or ""
    lyrics = bool(row.get("lyrics") or row.get("lyrics_clean"))
    lang = row.get("language") or row.get("language_cld3") or row.get("language_ft")
    if not artist or not title:
        return None
    return str(artist), str(title), lyrics, str(lang) if lang else None


def _adapt_lrclib(row: dict[str, Any]) -> tuple[str, str, bool, str | None] | None:
    """ernestchu/lrclib-20250319: artist_name, name (title), plain_lyrics / synced_lyrics."""
    artist = row.get("artist_name") or ""
    title = row.get("name") or ""
    has_plain = bool(row.get("has_plain_lyrics")) or bool(row.get("plain_lyrics"))
    has_synced = bool(row.get("has_synced_lyrics")) or bool(row.get("synced_lyrics"))
    lyrics = has_plain or has_synced
    if not artist or not title:
        return None
    return str(artist), str(title), lyrics, None


DATASETS: list[dict[str, Any]] = [
    {
        "name": "sebastiandizon/genius-song-lyrics",
        "adapter": _adapt_genius_standard,
        "load_kwargs": {"split": "train"},
    },
    {
        "name": "Dr3dre/Genius-song-lyrics-cleaned",
        "adapter": _adapt_genius_standard,
        "load_kwargs": {"split": "train"},
    },
    {
        "name": "theelderemo/genius-lyrics-cleaned",
        "adapter": _adapt_genius_standard,
        "load_kwargs": {"split": "train"},
    },
    {
        "name": "ernestchu/lrclib-20250319",
        "adapter": _adapt_lrclib,
        "load_kwargs": {"split": "train"},
    },
]


# ---------------------------------------------------------------------------

def load_seed_keys() -> tuple[dict[tuple[str, str], tuple[str, str]], int]:
    """Return {(norm_artist, norm_title): (raw_artist, raw_title)} for every seed row."""
    df = pd.read_csv(SEED_LIST_PATH)
    keys: dict[tuple[str, str], tuple[str, str]] = {}
    for _, row in df.iterrows():
        artist = str(row["artist"]) if pd.notna(row["artist"]) else ""
        title = str(row["title"]) if pd.notna(row["title"]) else ""
        key = (normalize_for_dedup(artist), normalize_for_dedup(title))
        keys[key] = (artist, title)
    return keys, len(df)


def eval_dataset(cfg: dict[str, Any], seed_keys: dict[tuple[str, str], tuple[str, str]]) -> None:
    name: str = cfg["name"]
    adapter = cfg["adapter"]
    load_kwargs: dict[str, Any] = cfg["load_kwargs"]

    print(f"\n{'='*70}")
    print(f"Dataset: {name}")
    print(f"{'='*70}")

    try:
        ds = load_dataset(name, streaming=True, trust_remote_code=True, **load_kwargs)
    except Exception as exc:
        print(f"  LOAD FAILED: {exc}")
        return

    # Peek at schema from first row
    first_row: dict[str, Any] | None = None
    columns: list[str] = []
    try:
        first_row = next(iter(ds))
        columns = list(first_row.keys())
    except StopIteration:
        print("  Dataset appears empty.")
        return
    except Exception as exc:
        print(f"  Could not read first row: {exc}")
        return

    print(f"  Columns: {columns}")

    # Detect lyrics field name
    lyrics_col = next(
        (c for c in columns if c.lower() in ("plain_lyrics", "synced_lyrics", "lyrics_clean", "lyrics", "text")),
        None,
    )
    print(f"  Lyrics column: {lyrics_col!r}")
    lang_col = next((c for c in columns if "lang" in c.lower()), None)
    print(f"  Language column: {lang_col!r}")

    # Stream up to SAMPLE_SIZE rows
    hits: dict[tuple[str, str], tuple[str, str]] = {}  # norm_key → (raw_artist, raw_title)
    rows_processed = 0
    lyrics_present_count = 0
    errors = 0

    try:
        for row in ds:
            if rows_processed >= SAMPLE_SIZE:
                break
            try:
                parsed = adapter(row)
                if parsed is None:
                    rows_processed += 1
                    continue
                raw_artist, raw_title, has_lyrics, _ = parsed
                if has_lyrics:
                    lyrics_present_count += 1
                key = (normalize_for_dedup(raw_artist), normalize_for_dedup(raw_title))
                if key in seed_keys and key not in hits:
                    hits[key] = (raw_artist, raw_title)
            except Exception:
                errors += 1
            rows_processed += 1
    except Exception as exc:
        print(f"  Stream error after {rows_processed} rows: {exc}")

    coverage = len(hits)
    total_seed = len(seed_keys)
    pct = coverage / total_seed * 100 if total_seed else 0

    print(f"\n  Rows sampled:      {rows_processed:,}")
    print(f"  Rows with lyrics:  {lyrics_present_count:,}  ({lyrics_present_count/max(rows_processed,1)*100:.1f}%)")
    print(f"  Parse errors:      {errors}")
    print(f"\n  Seed list size:    {total_seed:,}")
    print(f"  Coverage count:    {coverage:,}")
    print(f"  Coverage %:        {pct:.1f}%")
    print(f"  (lower-bound — sampled {rows_processed:,} of potentially larger dataset)")

    # 5 example matches
    hit_list = list(hits.values())
    print("\n  5 example MATCHES:")
    for raw_artist, raw_title in hit_list[:5]:
        print(f"    {raw_artist!r} — {raw_title!r}")

    # 5 example misses
    misses = [(a, t) for k, (a, t) in seed_keys.items() if k not in hits]
    print("\n  5 example MISSES (seed songs not found in sample):")
    for raw_artist, raw_title in misses[:5]:
        print(f"    {raw_artist!r} — {raw_title!r}")


def main() -> None:
    print(f"Loading seed list from {SEED_LIST_PATH} …")
    seed_keys, seed_total = load_seed_keys()
    print(f"Seed list: {seed_total:,} unique (artist, title) pairs loaded.")

    for cfg in DATASETS:
        eval_dataset(cfg, seed_keys)

    print(f"\n{'='*70}")
    print("Done. Streaming gives a lower-bound estimate.")
    print("Run full download on the best candidate for accurate numbers.")


if __name__ == "__main__":
    main()
