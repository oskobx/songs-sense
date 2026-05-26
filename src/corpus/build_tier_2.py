"""Build data/tier_2_decades.csv from Billboard Year-End Hot 100 (1960-2019).

Scrapes Wikipedia for each year's Year-End Hot 100 chart, then selects the
top-N unique songs per decade by best rank achieved within that decade.

Run from the project root:
    uv run python -m src.corpus.build_tier_2
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import httpx
import pandas as pd
from bs4 import BeautifulSoup

from .cleaning import normalize_for_dedup, parse_featured_artists

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUTPUT_PATH = Path("data/tier_2_decades.csv")
TIER1_PATH = Path("data/tier_1_canonical.csv")

DECADES: dict[str, range] = {
    "1960s": range(1960, 1970),
    "1970s": range(1970, 1980),
    "1980s": range(1980, 1990),
    "1990s": range(1990, 2000),
    "2000s": range(2000, 2010),
    "2010s": range(2010, 2020),
}

DECADE_TARGETS: dict[str, int] = {
    "1960s": 150,
    "1970s": 150,
    "1980s": 170,
    "1990s": 180,
    "2000s": 180,
    "2010s": 170,
}

_VERSION_RE = re.compile(
    r"\b(live|remix|acoustic|demo|instrumental|reprise|radio\s+edit)\b",
    re.IGNORECASE,
)
_FOOTNOTE_RE = re.compile(r"\[.*?\]")
_QUOTE_RE = re.compile(r'^["“”]+|["“”]+$')


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------


def _wiki_url(year: int) -> str:
    return f"https://en.wikipedia.org/wiki/Billboard_Year-End_Hot_100_singles_of_{year}"


def _clean_cell(text: str) -> str:
    """Strip Wikipedia footnote markers, surrounding quotes, and extra whitespace."""
    text = _FOOTNOTE_RE.sub("", text)
    # Collapse runs of whitespace that can appear when multiple anchor tags are
    # joined with separator=" " (e.g. "Lil Nas X  featuring  Billy Ray Cyrus").
    text = re.sub(r"\s+", " ", text).strip()
    text = _QUOTE_RE.sub("", text).strip()
    return text


def _find_col_indices(header_row) -> tuple[int, int, int] | None:
    """Return (rank_idx, title_idx, artist_idx) from a <tr>, or None if not found.

    Handles the header variations seen across Billboard Year-End Wikipedia articles:
    "No." / "Rank" / "#" for rank; "Title" / "Song" / "Single" for title;
    "Artist" / "Artist(s)" / "Performer" / "Performer(s)" / "Act" for artist.
    """
    cells = header_row.find_all(["th", "td"])
    headers = [c.get_text(strip=True).lower() for c in cells]

    rank_idx = title_idx = artist_idx = None
    for i, h in enumerate(headers):
        if rank_idx is None and h in ("no.", "no", "#", "rank"):
            rank_idx = i
        elif title_idx is None and any(kw in h for kw in ("title", "song", "single")):
            title_idx = i
        elif artist_idx is None and any(
            kw in h for kw in ("artist", "performer", "act")
        ):
            artist_idx = i

    if any(x is None for x in (rank_idx, title_idx, artist_idx)):
        return None
    return rank_idx, title_idx, artist_idx


def _parse_table_rows(
    rows: list, rank_idx: int, title_idx: int, artist_idx: int, year: int
) -> list[dict]:
    """Extract records from table rows given column indices. Skips non-data rows."""
    records: list[dict] = []
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) <= max(rank_idx, title_idx, artist_idx):
            continue
        rank_text = _clean_cell(cells[rank_idx].get_text(separator=" "))
        title_text = _clean_cell(cells[title_idx].get_text(separator=" "))
        artist_text = _clean_cell(cells[artist_idx].get_text(separator=" "))
        try:
            rank = int(rank_text)
        except ValueError:
            continue
        if not title_text or not artist_text:
            continue
        records.append({"rank": rank, "title": title_text, "artist_raw": artist_text, "year": year})
    return records


def _try_table(table, year: int) -> list[dict]:
    """Try to extract records from a BeautifulSoup table element.

    Attempts header-based column detection (rows 0 and 1), then falls back to
    positional layout (col 0 = rank, col 1 = title, col 2 = artist) which is
    the standard format for all Billboard Year-End Hot 100 Wikipedia articles.
    """
    rows = table.find_all("tr")
    if not rows:
        return []

    # Header-based detection (try first two rows for multi-row headers).
    col_indices = _find_col_indices(rows[0])
    if col_indices is None and len(rows) > 1:
        col_indices = _find_col_indices(rows[1])

    if col_indices is not None:
        records = _parse_table_rows(rows, *col_indices, year)
        if len(records) >= 50:
            return records

    # Positional fallback: standard layout is rank=0, title=1, artist=2.
    records = _parse_table_rows(rows, 0, 1, 2, year)
    if len(records) >= 50:
        return records

    return []


def scrape_year(year: int, client: httpx.Client) -> list[dict]:
    """Fetch and parse one year's Year-End Hot 100 from Wikipedia.

    Returns a list of dicts with keys: rank, title, artist_raw, year.
    Returns an empty list and prints a warning if the page can't be parsed.
    """
    url = _wiki_url(year)
    try:
        resp = client.get(url, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"  WARNING: HTTP error for {year}: {exc}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Try wikitables first (most years), then any table as last resort.
    for table in soup.find_all("table", class_=re.compile(r"wikitable")):
        records = _try_table(table, year)
        if records:
            return records

    for table in soup.find_all("table"):
        records = _try_table(table, year)
        if records:
            return records

    print(f"  WARNING: Could not parse a valid table for {year}")
    return []


def scrape_all_years() -> pd.DataFrame:
    """Scrape Wikipedia for all years 1960-2019 and return a raw DataFrame."""
    all_rows: list[dict] = []
    headers = {
        "User-Agent": "songs-sense-bot/1.0 (educational project; contact: oskobx@gmail.com)"
    }
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for decade, years in DECADES.items():
            print(f"\nScraping {decade}...")
            for year in years:
                rows = scrape_year(year, client)
                all_rows.extend(rows)
                print(f"  {year}: {len(rows)} songs")
                time.sleep(0.5)  # polite crawl delay

    return pd.DataFrame(all_rows)


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


def process_raw(raw: pd.DataFrame) -> pd.DataFrame:
    """Parse artist strings, compute decade labels and scores."""
    records = []
    for _, row in raw.iterrows():
        artist, featured = parse_featured_artists(str(row["artist_raw"]))
        year = int(row["year"])
        records.append(
            {
                "artist": artist.strip(),
                "featured_artists": featured,
                "title": str(row["title"]).strip(),
                "year": year,
                "rank": int(row["rank"]),
                "score": 101 - int(row["rank"]),  # rank 1 → 100, rank 100 → 1
                "decade": f"{(year // 10) * 10}s",
            }
        )
    return pd.DataFrame(records)


def _drop_alternate_versions(df: pd.DataFrame) -> pd.DataFrame:
    """Drop live/remix/demo versions when the original exists in the same frame.

    Requires _key and _artist_key columns to be present (added by the caller).
    """
    is_alt = df["title"].apply(lambda t: bool(_VERSION_RE.search(t)))
    base_keys: set[str] = set(df.loc[~is_alt, "_key"])

    to_drop: list = []
    for idx, row in df[is_alt].iterrows():
        # Strip trailing parenthetical/bracketed annotation to get the base title.
        stripped = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", row["title"]).strip()
        candidate = row["_artist_key"] + "|||" + normalize_for_dedup(stripped)
        if candidate in base_keys:
            to_drop.append(idx)

    return df.drop(index=to_drop)


def _aggregate_decade(sub: pd.DataFrame) -> pd.DataFrame:
    """Collapse per-appearance rows into one row per unique song.

    For each song:
    - decade_score = sum of (101 - rank) across all year-end appearances
    - year         = the year where rank was best (lowest number)
    - artist / featured_artists / title taken from the best-rank appearance

    Requires _key, _artist_key columns to be pre-computed on sub.
    """
    rows: list[dict] = []
    for key, group in sub.groupby("_key", sort=False):
        best = group.loc[group["rank"].idxmin()]
        rows.append(
            {
                "_key": key,
                "_artist_key": best["_artist_key"],
                "artist": best["artist"],
                "featured_artists": best["featured_artists"],
                "title": best["title"],
                "year": int(best["year"]),
                "decade": best["decade"],
                "decade_score": int(group["score"].sum()),
            }
        )
    return pd.DataFrame(rows)


def build_decade_top(
    df: pd.DataFrame, t1_keys: set[str]
) -> tuple[pd.DataFrame, int]:
    """Select the top-N songs per decade by cumulative decade score.

    Decade score = sum of (101 - rank) across every year-end chart appearance
    within the decade. Rewards sustained presence as well as peak performance.

    Tier 1 songs are excluded before per-decade selection rather than after,
    so the target counts are met by the best non-Tier-1 songs rather than by
    trimming the final list (which would leave the least-known decades under-
    represented because the all-time Billboard classics overlap heavily with
    the most popular year-end Hot 100 entries).

    Returns (tier2_df_with_key_col, n_candidates_excluded_by_t1).
    """
    result_frames: list[pd.DataFrame] = []
    excluded_total = 0

    for decade, target in DECADE_TARGETS.items():
        sub = df[df["decade"] == decade].copy()
        sub["_artist_key"] = sub["artist"].apply(normalize_for_dedup)
        sub["_title_key"] = sub["title"].apply(normalize_for_dedup)
        sub["_key"] = sub["_artist_key"] + "|||" + sub["_title_key"]

        # Aggregate appearances → one row per song with cumulative decade score.
        agg = _aggregate_decade(sub)
        agg = _drop_alternate_versions(agg)

        # Exclude Tier 1 songs before selecting top-N.
        in_t1 = agg["_key"].isin(t1_keys)
        excluded_total += int(in_t1.sum())
        candidates = agg[~in_t1]

        result_frames.append(candidates.nlargest(target, "decade_score"))

    combined = pd.concat(result_frames, ignore_index=True)

    # Cross-decade dedup: keep the entry with the higher cumulative decade score.
    combined["_artist_key"] = combined["artist"].apply(normalize_for_dedup)
    combined["_title_key"] = combined["title"].apply(normalize_for_dedup)
    combined["_key"] = combined["_artist_key"] + "|||" + combined["_title_key"]
    combined = combined.sort_values("decade_score", ascending=False).drop_duplicates(
        subset="_key", keep="first"
    )

    combined["source"] = "billboard_year_end"
    combined["source_rank"] = combined["decade_score"]
    combined["tier"] = "decades"

    result = combined[
        ["artist", "featured_artists", "title", "year", "decade", "source", "source_rank", "tier", "_key"]
    ].reset_index(drop=True)

    return result, excluded_total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("=== Tier 2: Billboard Year-End Hot 100 (1960-2019) ===")
    print("(Wikipedia scrape — expect ~30 s for 60 HTTP requests)\n")

    raw = scrape_all_years()
    print(f"\nTotal raw rows scraped: {len(raw)}")

    print("\nProcessing raw data...")
    processed = process_raw(raw)

    print(f"Loading Tier 1 exclusion set from {TIER1_PATH}...")
    t1 = pd.read_csv(TIER1_PATH)
    t1_keys: set[str] = {
        normalize_for_dedup(str(r["artist"])) + "|||" + normalize_for_dedup(str(r["title"]))
        for _, r in t1.iterrows()
    }
    print(f"  {len(t1_keys)} Tier 1 songs will be excluded from the candidate pool")

    print("Selecting top songs per decade (Tier 1 excluded before selection)...")
    tier2, n_excluded = build_decade_top(processed, t1_keys)

    print(f"  {n_excluded} unique Tier 1 songs were in the candidate pool and excluded")

    print("\nSongs per decade:")
    for decade in DECADE_TARGETS:
        n = (tier2["decade"] == decade).sum()
        target = DECADE_TARGETS[decade]
        print(f"  {decade}: {n}  (target ~{target})")

    final = tier2.drop(columns=["_key"]).reset_index(drop=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    final.to_csv(OUTPUT_PATH, index=False)

    feat_count = int(final["featured_artists"].notna().sum())

    print("\n=== Summary ===")
    print(f"  Tier 1 candidates excluded: {n_excluded}")
    print(f"  Final row count:            {len(final)}")
    print(f"  Rows with featured art.:    {feat_count}")
    print(f"  Output: {OUTPUT_PATH}")

    # ------------------------------------------------------------------
    # Success criteria
    # ------------------------------------------------------------------
    print("\n=== Success Criteria ===")
    all_ok = True

    artist_keys = final["artist"].apply(normalize_for_dedup)
    title_keys = final["title"].apply(normalize_for_dedup)

    count_ok = 700 <= len(final) <= 1000
    print(f"  {'OK' if count_ok else 'FAIL'} Row count {len(final)} (expected 700-1000)")
    all_ok = all_ok and count_ok

    no_dupes = not final.duplicated(subset=["artist", "title"]).any()
    print(f"  {'OK' if no_dupes else 'FAIL'} No exact (artist, title) duplicates")
    all_ok = all_ok and no_dupes

    t1_akeys = t1["artist"].apply(normalize_for_dedup)
    t1_tkeys = t1["title"].apply(normalize_for_dedup)

    sanity_checks = [
        ("I Heard It Through the Grapevine", "Marvin Gaye"),
        ("Stayin' Alive", "Bee Gees"),
        ("Billie Jean", "Michael Jackson"),
        ("I Will Always Love You", "Whitney Houston"),
        ("Hey Ya!", "OutKast"),
        ("Uptown Funk", "Mark Ronson"),
    ]

    found = 0
    for title, artist in sanity_checks:
        ak = normalize_for_dedup(artist)
        tk = normalize_for_dedup(title)
        in_t2 = ((artist_keys == ak) & (title_keys == tk)).any()
        in_t1 = ((t1_akeys == ak) & (t1_tkeys == tk)).any()
        if in_t2:
            print(f"  OK  '{title}' by {artist} — in Tier 2")
            found += 1
        elif in_t1:
            print(f"  OK  '{title}' by {artist} — in Tier 1 (correctly dedup'd out)")
            found += 1
        else:
            print(f"  FAIL '{title}' by {artist} — not found in either tier")

    sanity_ok = found >= 3
    print(f"  {'OK' if sanity_ok else 'FAIL'} Sanity checks: {found}/6 found (need ≥3)")
    all_ok = all_ok and sanity_ok

    feat_ok = feat_count > 0
    print(f"  {'OK' if feat_ok else 'FAIL'} Has featured-artist rows ({feat_count})")
    all_ok = all_ok and feat_ok

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
