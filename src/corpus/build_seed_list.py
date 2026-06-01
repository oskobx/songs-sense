"""Merge the six tier CSVs into data/seed_list.csv.

Run from the project root:
    uv run python -m src.corpus.build_seed_list
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .cleaning import normalize_for_dedup

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

TIERS: list[tuple[str, str]] = [
    ("tier_1_canonical.csv", "canonical"),
    ("tier_2_decades.csv", "decades"),
    ("tier_3_recent.csv", "recent"),
    ("tier_4_genres.csv", "genre"),
    ("tier_5_viral.csv", "viral"),
    ("tier_6_favorites.csv", "favorites"),
]

SCHEMA: list[str] = [
    "artist",
    "featured_artists",
    "title",
    "year",
    "decade",
    "genre",
    "original_year",
    "genius_id",
    "source",
    "source_rank",
    "tier",
]


def _load_tier(filename: str, tier_label: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / filename, dtype=str)
    df["tier"] = tier_label
    for col in SCHEMA:
        if col not in df.columns:
            df[col] = pd.NA
    return df[SCHEMA]


def main() -> None:
    frames: list[pd.DataFrame] = []
    tier_counts: dict[str, int] = {}

    for filename, label in TIERS:
        df = _load_tier(filename, label)
        tier_counts[label] = len(df)
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True)
    total_before = len(combined)

    # Safety-net dedup: keep row from the earliest tier on (artist, title) collision.
    tier_order = {label: i for i, (_, label) in enumerate(TIERS)}
    combined["_order"] = combined["tier"].map(tier_order)
    combined["_key"] = (
        combined["artist"].fillna("").apply(normalize_for_dedup)
        + "|||"
        + combined["title"].fillna("").apply(normalize_for_dedup)
    )
    combined = combined.sort_values("_order")
    deduped = combined.drop_duplicates(subset="_key", keep="first").copy()
    dupes_removed = total_before - len(deduped)
    deduped.drop(columns=["_order", "_key"], inplace=True)

    # Cast to appropriate dtypes (Int64 = nullable integer, float for source_rank).
    for col in ("year", "original_year", "genius_id"):
        deduped[col] = pd.to_numeric(deduped[col], errors="coerce").astype("Int64")
    deduped["source_rank"] = pd.to_numeric(deduped["source_rank"], errors="coerce")

    out_path = DATA / "seed_list.csv"
    deduped.to_csv(out_path, index=False)

    # ── Report ───────────────────────────────────────────────────────────────
    print("Per-tier counts (input):")
    for label, count in tier_counts.items():
        print(f"  {label:<12}  {count:>5}")

    print(f"\nTotal before safety-net dedup : {total_before}")
    print(f"Duplicates removed            : {dupes_removed}")
    print(f"Total final count             : {len(deduped)}")

    print("\nNull-count breakdown:")
    for col in SCHEMA:
        n = int(deduped[col].isna().sum())
        pct = n / len(deduped) * 100
        print(f"  {col:<20}  {n:>6}  ({pct:.1f}%)")

    print(f"\nWrote → {out_path}")


if __name__ == "__main__":
    main()
