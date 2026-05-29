"""Build data/tier_6_favorites.csv — full discographies of 8 personally-chosen artists.

Source: Genius API (/artists/{id}/songs with pagination).
Filters to songs where the queried artist is primary (not just featured).
Includes the new genius_id column for later lyrics fetching.

Run from project root:
    uv run python -m src.corpus.build_tier_6           # full run
    uv run python -m src.corpus.build_tier_6 --probe  # Lil Peep only, first 20 songs
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

from .cleaning import normalize_for_dedup

load_dotenv()

OUTPUT_PATH = Path("data/tier_6_favorites.csv")
TIER_PATHS = [
    Path("data/tier_1_canonical.csv"),
    Path("data/tier_2_decades.csv"),
    Path("data/tier_3_recent.csv"),
    Path("data/tier_4_genres.csv"),
    Path("data/tier_5_viral.csv"),
]

GENIUS_TOKEN = os.getenv("GENIUS_API_TOKEN") or os.getenv("GENIUS_TOKEN", "")
GENIUS_BASE = "https://api.genius.com"

ARTISTS: list[str] = [
    "Lil Peep",
    "Sentino",
    "Malik Montana",
    "Gang Albanii",
    "Lil Uzi Vert",
    "Rammstein",
    "Crystal Castles",
    "Depeche Mode",
]

# Per-artist hard cap applied after cleaning. Songs with known years are kept first
# (more likely officially released), then by year descending, then yearless songs.
ARTIST_CAPS: dict[str, int] = {
    "Lil Uzi Vert": 500,
}

# Rammstein: artist's primary language is German; drop English/USA edit versions
_RAMMSTEIN_FOREIGN_RE = re.compile(
    r"\b(english\s+version|usa\s+edit|english\s+radio\s+edit|radio\s+edit\s+english)\b",
    re.IGNORECASE,
)

# Tier-6 variant filter: unconditionally drop any title matching these patterns.
#
# "mix" uses a negative lookahead so compound words like "Mix-Up" / "Mix-Down"
# survive (hyphen immediately after "mix" followed by a word char = keep).
# \bdemo\b avoids "Demon"; \bedit\b avoids "Edition"; \blive\b avoids "Alive".
# rmx covers "(RMX by ...)" abbreviation used on Genius.
_VARIANT_RE = re.compile(
    r"\b(?:remix|rmx|edit|live|acoustic|demo|instrumental|remaster(?:ed)?|version|rework|re[\s\-]?recorded)\b"
    r"|\bmix\b(?!-\w)",
    re.IGNORECASE,
)

# Genius meta-entries that aren't songs
_GENIUS_META_RE = re.compile(
    r"\b("
    r"genius\s+(annotation|commentary|translation|english\s+translation)"
    r"|translated\s+by"
    r"|producers?\s+credits?"
    r"|writers?\s+(and\s+)?producers?"
    r"|behind\s+the\s+scenes"
    r"|liner\s+notes"
    r")\b",
    re.IGNORECASE,
)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GENIUS_TOKEN}",
        "User-Agent": "songs-sense-bot/1.0 (educational; oskobx@gmail.com)",
    }


# ---------------------------------------------------------------------------
# Artist ID lookup
# ---------------------------------------------------------------------------


def _find_artist_id(artist_name: str, client: httpx.Client) -> int | None:
    """Search Genius for songs by artist_name and return the primary_artist.id."""
    try:
        r = client.get(
            f"{GENIUS_BASE}/search",
            params={"q": artist_name, "per_page": 10},
            timeout=15,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"  ERROR searching for {artist_name!r}: {exc}")
        return None

    hits = r.json().get("response", {}).get("hits", [])
    artist_key = normalize_for_dedup(artist_name)

    for hit in hits:
        if hit.get("type") != "song":
            continue
        pa = hit.get("result", {}).get("primary_artist", {})
        if normalize_for_dedup(pa.get("name", "")) == artist_key:
            return pa.get("id")

    # Fuzzy fallback: accept if the queried name is a prefix of returned name
    for hit in hits:
        if hit.get("type") != "song":
            continue
        pa = hit.get("result", {}).get("primary_artist", {})
        pa_name = pa.get("name", "")
        if artist_key in normalize_for_dedup(pa_name):
            print(f"  WARNING: fuzzy match for {artist_name!r} → {pa_name!r}")
            return pa.get("id")

    print(f"  WARNING: no Genius artist_id found for {artist_name!r}")
    return None


# ---------------------------------------------------------------------------
# Discography fetch
# ---------------------------------------------------------------------------


def _fetch_artist_discography(
    artist_name: str,
    artist_id: int,
    client: httpx.Client,
) -> list[dict]:
    """Paginate /artists/{id}/songs and return songs where artist is primary."""
    songs: list[dict] = []
    page: int = 1
    artist_key = normalize_for_dedup(artist_name)

    while True:
        data: dict = {}
        for attempt in range(3):
            try:
                r = client.get(
                    f"{GENIUS_BASE}/artists/{artist_id}/songs",
                    params={"sort": "popularity", "per_page": 50, "page": page},
                    timeout=20,
                )
                r.raise_for_status()
                data = r.json().get("response", {})
                break
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == 2:
                    print(f"  ERROR fetching page {page} for {artist_name!r}: {exc}")
                    data = {}
                else:
                    time.sleep(2 ** attempt)
        if not data:
            break
        page_songs = data.get("songs", [])
        next_page = data.get("next_page")

        for song in page_songs:
            # Only include songs where this artist is listed as primary
            pa = song.get("primary_artist", {})
            if normalize_for_dedup(pa.get("name", "")) != artist_key:
                continue

            title = (song.get("title") or "").strip()
            if not title:
                continue

            feat_list = song.get("featured_artists") or []
            featured = ", ".join(a["name"] for a in feat_list if a.get("name")) or None

            rdc = song.get("release_date_components") or {}
            year = rdc.get("year")

            songs.append({
                "artist": artist_name,
                "featured_artists": featured,
                "title": title,
                "year": year,
                "genius_id": song.get("id"),
            })

        if next_page is None:
            break
        page = next_page
        time.sleep(0.4)

    return songs


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


def _clean_songs(artist_name: str, songs: list[dict]) -> list[dict]:
    """Apply tier-6 cleaning rules to one artist's song list."""
    # Drop Genius meta-entries
    before = len(songs)
    songs = [s for s in songs if not _GENIUS_META_RE.search(s["title"])]
    if (dropped := before - len(songs)):
        print(f"    Dropped {dropped} Genius meta-entries")

    # Rammstein: drop English-language versions of German songs not caught by _VARIANT_RE
    if artist_name == "Rammstein":
        before = len(songs)
        songs = [s for s in songs if not _RAMMSTEIN_FOREIGN_RE.search(s["title"])]
        if (dropped := before - len(songs)):
            print(f"    Dropped {dropped} Rammstein foreign-language versions")

    # Unconditionally drop any title that looks like a variant
    before = len(songs)
    songs = [s for s in songs if not _VARIANT_RE.search(s["title"])]
    if (dropped := before - len(songs)):
        print(f"    Dropped {dropped} variant titles (remix/edit/live/mix/version/…)")

    return songs


def _dedup_within_tier(songs: list[dict]) -> list[dict]:
    """Deduplicate by (artist, normalized_title), keeping first occurrence."""
    seen: set[str] = set()
    result: list[dict] = []
    for s in songs:
        key = normalize_for_dedup(s["artist"]) + "|||" + normalize_for_dedup(s["title"])
        if key not in seen:
            seen.add(key)
            result.append(s)
    return result


# ---------------------------------------------------------------------------
# Prior-tier dedup helpers
# ---------------------------------------------------------------------------


def _load_tier_keys(path: Path) -> set[str]:
    if not path.exists():
        print(f"  WARNING: {path} not found — skipping dedup against it")
        return set()
    df = pd.read_csv(path)
    return {
        normalize_for_dedup(str(r["artist"])) + "|||" + normalize_for_dedup(str(r["title"]))
        for _, r in df.iterrows()
    }


def _dedup_against_tiers(
    df: pd.DataFrame,
    tier_keys: list[tuple[str, set[str]]],
) -> pd.DataFrame:
    df = df.copy()
    df["_key"] = df["artist"].apply(normalize_for_dedup) + "|||" + df["title"].apply(normalize_for_dedup)

    for tier_name, keys in tier_keys:
        mask = df["_key"].isin(keys)
        n = int(mask.sum())
        df = df[~mask].copy()
        print(f"  Removed {n} rows already in {tier_name}")

    return df.drop(columns=["_key"])


# ---------------------------------------------------------------------------
# Probe mode: Lil Peep first 20 songs
# ---------------------------------------------------------------------------


def _probe(client: httpx.Client) -> None:
    print("=== Tier 6 PROBE: Lil Peep discography (first 20 songs) ===\n")

    if not GENIUS_TOKEN:
        print("ERROR: GENIUS_API_TOKEN not set in .env")
        sys.exit(1)

    print("Searching for Lil Peep on Genius...")
    artist_id = _find_artist_id("Lil Peep", client)
    if artist_id is None:
        print("ERROR: could not find Lil Peep on Genius")
        sys.exit(1)
    print(f"Found artist_id = {artist_id}\n")

    print("Fetching discography...")
    songs = _fetch_artist_discography("Lil Peep", artist_id, client)
    print(f"Raw songs where Lil Peep is primary artist: {len(songs)}\n")

    df = pd.DataFrame(songs)
    print("First 20 songs (unsorted):")
    print(
        df[["title", "year", "genius_id", "featured_artists"]]
        .head(20)
        .to_string(index=True)
    )
    print(
        "\nVerify these look like real Lil Peep songs, "
        "then run without --probe for the full build."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if not GENIUS_TOKEN:
        print("ERROR: GENIUS_API_TOKEN not set in .env")
        sys.exit(1)

    with httpx.Client(headers=_headers(), follow_redirects=True) as client:
        if "--probe" in sys.argv:
            _probe(client)
            return

        print("=== Tier 6: Personal Favorites ===")
        print(f"Artists: {', '.join(ARTISTS)}\n")

        # ── Fetch and clean per artist ────────────────────────────────────
        raw_counts: dict[str, int] = {}
        cleaned_counts: dict[str, int] = {}
        all_songs: list[dict] = []

        for artist_name in ARTISTS:
            print(f"\n[{artist_name}]")
            artist_id = _find_artist_id(artist_name, client)
            if artist_id is None:
                print(f"  SKIPPED: no Genius artist_id found")
                raw_counts[artist_name] = 0
                cleaned_counts[artist_name] = 0
                continue
            print(f"  artist_id = {artist_id}")
            time.sleep(0.3)

            raw = _fetch_artist_discography(artist_name, artist_id, client)
            raw_counts[artist_name] = len(raw)
            print(f"  Raw (primary-artist songs): {len(raw)}")

            cleaned = _clean_songs(artist_name, raw)
            cleaned = _dedup_within_tier(cleaned)

            cap = ARTIST_CAPS.get(artist_name)
            if cap and len(cleaned) > cap:
                dropped = len(cleaned) - cap
                cleaned = cleaned[:cap]  # Genius returns by popularity; take the top N
                print(f"  Capped at {cap} (dropped {dropped} less-popular songs)")

            cleaned_counts[artist_name] = len(cleaned)
            print(f"  After cleaning + within-tier dedup: {len(cleaned)}")

            all_songs.extend(cleaned)

        # ── Cross-artist dedup within Tier 6 ─────────────────────────────
        print(f"\nTotal before cross-artist dedup: {len(all_songs)}")
        all_songs = _dedup_within_tier(all_songs)
        print(f"After cross-artist dedup: {len(all_songs)}")

        # ── Dedup against Tiers 1-5 ───────────────────────────────────────
        print("\nDeduplicating against prior tiers...")
        tier_keys = [
            (f"Tier {i + 1}", _load_tier_keys(path))
            for i, path in enumerate(TIER_PATHS)
        ]

        df = pd.DataFrame(all_songs)
        df["source"] = "genius_discography"
        df["source_rank"] = 1.0
        df["tier"] = "favorites"

        pre_dedup = len(df)
        df = _dedup_against_tiers(df, tier_keys)
        post_dedup_counts: dict[str, int] = {}
        for artist_name in ARTISTS:
            post_dedup_counts[artist_name] = int((df["artist"] == artist_name).sum())

        # ── Finalise ──────────────────────────────────────────────────────
        final = df[
            ["artist", "featured_artists", "title", "year", "genius_id",
             "source", "source_rank", "tier"]
        ].reset_index(drop=True)

        # Ensure genius_id is integer where present
        final["genius_id"] = pd.to_numeric(final["genius_id"], errors="coerce").astype("Int64")

        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        final.to_csv(OUTPUT_PATH, index=False)

        # ── Summary ───────────────────────────────────────────────────────
        print(f"\n=== Summary ===")
        print(f"{'Artist':<20} {'Raw':>6} {'Cleaned':>9} {'Final':>7}")
        print("-" * 46)
        for a in ARTISTS:
            raw_n = raw_counts.get(a, 0)
            cln_n = cleaned_counts.get(a, 0)
            fin_n = post_dedup_counts.get(a, 0)
            flag = " ← <30 rows" if fin_n < 30 else ""
            print(f"  {a:<18} {raw_n:>6} {cln_n:>9} {fin_n:>7}{flag}")
        print("-" * 46)
        print(f"  {'TOTAL':<18} {sum(raw_counts.values()):>6} {sum(cleaned_counts.values()):>9} {len(final):>7}")
        print(f"\nOutput: {OUTPUT_PATH}")

        # ── Success criteria ──────────────────────────────────────────────
        print("\n=== Success Criteria ===")
        all_ok = True

        count_ok = 700 <= len(final) <= 5000
        print(f"  {'OK' if count_ok else 'FAIL'} Total rows {len(final)} (target ≥700)")
        all_ok = all_ok and count_ok

        no_dupes = not final.duplicated(subset=["artist", "title"]).any()
        print(f"  {'OK' if no_dupes else 'FAIL'} No exact (artist, title) duplicates")
        all_ok = all_ok and no_dupes

        all_genius_ids = final["genius_id"].notna().all()
        print(f"  {'OK' if all_genius_ids else 'FAIL'} All rows have non-null genius_id")
        all_ok = all_ok and all_genius_ids

        for a in ARTISTS:
            n = post_dedup_counts.get(a, 0)
            # Gang Albanii has thin Genius coverage (Polish rap); exempt from the ≥30 floor
            ok = n >= 30 or a == "Gang Albanii"
            print(f"  {'OK' if ok else 'FAIL'} {a}: {n} rows{' (Polish coverage thin — no minimum)' if a == 'Gang Albanii' else ' (need ≥30)'}")
            all_ok = all_ok and ok

        tier_keys_flat = {k for _, ks in tier_keys for k in ks}
        final_keys = (
            final["artist"].apply(normalize_for_dedup)
            + "|||"
            + final["title"].apply(normalize_for_dedup)
        )
        no_prior_overlap = not final_keys.isin(tier_keys_flat).any()
        print(f"  {'OK' if no_prior_overlap else 'FAIL'} No Tier 1-5 songs in Tier 6")
        all_ok = all_ok and no_prior_overlap

        if not all_ok:
            print("\nSome criteria not met — see counts above.")
            sys.exit(1)
        else:
            print("\nAll success criteria met.")


if __name__ == "__main__":
    main()
