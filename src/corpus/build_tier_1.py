"""Build data/tier_1_canonical.csv from Rolling Stone 500 and Billboard GOAT.

Run from the project root:
    uv run python -m src.corpus.build_tier_1
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import httpx
import pandas as pd

# Relative import works when run as `python -m src.corpus.build_tier_1`
from .cleaning import normalize_for_dedup, parse_featured_artists, smart_title_case

# ---------------------------------------------------------------------------
# Source URLs
# ---------------------------------------------------------------------------

RS500_URL = (
    "https://raw.githubusercontent.com/ossings/rolling_stone_top_500_songs_2021"
    "/main/top_500_songs.csv"
)

BILLBOARD_GOAT_URL = (
    "https://raw.githubusercontent.com/daveking63/Billboard-and-RIAA-datasets"
    "/master/BB-Top600_Songs.txt"
)

OUTPUT_PATH = Path("data/tier_1_canonical.csv")

# Canonical source order for the `source` column
_SOURCE_ORDER = ["rs500", "billboard_goat", "ifpi"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fetch_csv(url: str, **kwargs) -> pd.DataFrame:
    """Download a CSV/text file from a URL and return as a DataFrame."""
    response = httpx.get(url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    return pd.read_csv(io.StringIO(response.text), **kwargs)


# ---------------------------------------------------------------------------
# Source loaders
# ---------------------------------------------------------------------------


def load_rs500() -> pd.DataFrame:
    """Load Rolling Stone 500 (2021 revision) from GitHub CSV.

    Source columns: Rank, Title, Artist, Year
    """
    raw = _fetch_csv(RS500_URL)

    records = []
    for _, row in raw.iterrows():
        artist, featured = parse_featured_artists(str(row["Artist"]))
        year_val = row["Year"]
        records.append(
            {
                "artist": artist.strip(),
                "featured_artists": featured,
                "title": str(row["Title"]).strip(),
                "year": int(year_val) if pd.notna(year_val) else None,
                "source": "rs500",
                "source_rank": int(row["Rank"]),
                "tier": "canonical",
            }
        )
    return pd.DataFrame(records)


def load_billboard_goat() -> pd.DataFrame:
    """Load Billboard Greatest of All Time Hot 100 (2015 version) from GitHub.

    Source columns: Rank, Artist, SongTitle, Gender, Genre, Decade[, Count]
    Song titles are stored in ALL CAPS — we title-case them on load.
    Featured artists are embedded in Artist as "Artist Featuring X".
    No release year available (only decade), so year is always null here.
    """
    raw = _fetch_csv(BILLBOARD_GOAT_URL, usecols=["Rank", "Artist", "SongTitle"])

    records = []
    for _, row in raw.iterrows():
        artist_raw = str(row["Artist"]).strip()
        artist, featured = parse_featured_artists(artist_raw)

        title_raw = str(row["SongTitle"]).strip()
        # Titles are ALL CAPS in this dataset
        title = smart_title_case(title_raw) if title_raw == title_raw.upper() else title_raw

        records.append(
            {
                "artist": artist.strip(),
                "featured_artists": featured,
                "title": title,
                "year": None,
                "source": "billboard_goat",
                "source_rank": int(row["Rank"]),
                "tier": "canonical",
            }
        )
    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _merge_group(group: pd.DataFrame) -> dict:
    """Collapse a group of duplicate rows into one canonical row."""
    sources_present = set(
        s.strip() for val in group["source"] for s in val.split(",")
    )

    # For artist/title, prefer RS500 formatting; fall back to Billboard
    rs_rows = group[group["source"] == "rs500"]
    ref = rs_rows.iloc[0] if len(rs_rows) > 0 else group.iloc[0]

    # Featured artists: keep the longest (most complete) non-null value
    feat_series = group["featured_artists"].dropna()
    if len(feat_series) > 0:
        featured = feat_series.loc[feat_series.str.len().idxmax()]
    else:
        featured = None

    # Year: first non-null value
    year_series = group["year"].dropna()
    year = int(year_series.iloc[0]) if len(year_series) > 0 else None

    # Source: comma-separated in canonical order
    source_str = ", ".join(s for s in _SOURCE_ORDER if s in sources_present)

    # Source rank: best (lowest) across sources
    rank_series = group["source_rank"].dropna()
    source_rank = int(rank_series.min()) if len(rank_series) > 0 else None

    return {
        "artist": ref["artist"],
        "featured_artists": featured,
        "title": ref["title"],
        "year": year,
        "source": source_str,
        "source_rank": source_rank,
        "tier": "canonical",
    }


def dedup(df: pd.DataFrame) -> pd.DataFrame:
    """Merge rows that refer to the same song across sources.

    Dedup key: normalize_for_dedup(artist) + normalize_for_dedup(title).
    Featured artists are NOT part of the key per spec.
    """
    df = df.copy()
    df["_artist_key"] = df["artist"].apply(normalize_for_dedup)
    df["_title_key"] = df["title"].apply(normalize_for_dedup)
    df["_key"] = df["_artist_key"] + "|||" + df["_title_key"]

    merged = [_merge_group(group) for _, group in df.groupby("_key", sort=False)]
    result = pd.DataFrame(merged)
    return result.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("Loading Rolling Stone 500 (2021)...")
    rs500 = load_rs500()
    print(f"  {len(rs500)} songs")

    print("Loading Billboard Greatest of All Time Hot 100...")
    billboard = load_billboard_goat()
    print(f"  {len(billboard)} songs")

    combined = pd.concat([rs500, billboard], ignore_index=True)
    pre_dedup = len(combined)

    print("Deduplicating...")
    final = dedup(combined)

    # Convert year to nullable integer so CSV shows blank instead of "2007.0"
    final["year"] = final["year"].astype("Int64")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    final.to_csv(OUTPUT_PATH, index=False)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    feat_count = final["featured_artists"].notna().sum()
    dupes_removed = pre_dedup - len(final)

    print("\n=== Summary ===")
    print(f"  RS500 source songs:      {len(rs500)}")
    print(f"  Billboard GOAT songs:    {len(billboard)}")
    print(f"  Pre-dedup total:         {pre_dedup}")
    print(f"  Duplicates removed:      {dupes_removed}")
    print(f"  Final row count:         {len(final)}")
    print(f"  Rows with featured art.: {feat_count}")
    print(f"  Output: {OUTPUT_PATH}")

    # ------------------------------------------------------------------
    # Success-criteria check
    # ------------------------------------------------------------------
    required = [
        ("Bohemian Rhapsody", "Queen"),
        ("Like a Rolling Stone", "Bob Dylan"),
        ("Smells Like Teen Spirit", "Nirvana"),
        ("Respect", "Aretha Franklin"),
        ("Hey Jude", "The Beatles"),
    ]

    artist_keys = final["artist"].apply(normalize_for_dedup)
    title_keys = final["title"].apply(normalize_for_dedup)

    all_ok = True
    print("\n=== Success Criteria ===")

    count_ok = 900 <= len(final) <= 1100
    print(f"  {'OK' if count_ok else 'FAIL'} Row count {len(final)} (expected 900-1100)")
    all_ok = all_ok and count_ok

    for title, artist in required:
        found = (
            (artist_keys == normalize_for_dedup(artist))
            & (title_keys == normalize_for_dedup(title))
        ).any()
        print(f"  {'OK' if found else 'FAIL'} '{title}' by {artist}")
        all_ok = all_ok and found

    feat_ok = feat_count > 0
    print(f"  {'OK' if feat_ok else 'FAIL'} Has featured-artist rows ({feat_count})")
    all_ok = all_ok and feat_ok

    no_exact_dupes = not final.duplicated(subset=["artist", "title"]).any()
    print(f"  {'OK' if no_exact_dupes else 'FAIL'} No exact (artist, title) duplicates")
    all_ok = all_ok and no_exact_dupes

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
