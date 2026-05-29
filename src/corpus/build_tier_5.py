"""Build data/tier_5_viral.csv — viral/TikTok-era songs 2019-2026.

Sources:
  1. Wikipedia "TikTok Billboard Top 50" — weekly number-ones, 2023-2025
  2. TikTok newsroom "Year on TikTok" pages — 2023 and 2024 prose song lists
  3. kworb.net per-year Spotify top-200 songs — 2019-2025 (proxy for viral era)
  4. Wikipedia individual song article scan — back-catalog viral songs with original_year

No song titles or artist names are hardcoded. Every row comes from scraped sources.

Run from project root:
    uv run python -m src.corpus.build_tier_5           # full run
    uv run python -m src.corpus.build_tier_5 --probe  # first source (TikTok Billboard Top 50 2023), 20 rows
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import httpx
import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from .cleaning import normalize_for_dedup, parse_featured_artists

load_dotenv()

OUTPUT_PATH = Path("data/tier_5_viral.csv")
TIER1_PATH = Path("data/tier_1_canonical.csv")
TIER2_PATH = Path("data/tier_2_decades.csv")
TIER3_PATH = Path("data/tier_3_recent.csv")
TIER4_PATH = Path("data/tier_4_genres.csv")

VIRAL_YEARS = list(range(2019, 2027))
# Number of songs to take from each kworb yearly page
KWORB_LIMIT_PER_YEAR = 200

SOURCE_WEIGHTS: dict[str, float] = {
    "tiktok_year": 1.0,        # Year on TikTok official (TikTok newsroom)
    "spotify_viral": 0.8,      # Spotify top-streams proxy (kworb yearly)
    "billboard_trending": 0.7, # TikTok Billboard Top 50 (Wikipedia)
    "wiki_viral": 0.5,         # Wikipedia song articles mentioning TikTok
}
# Year-assignment priority: higher = more reliable viral year.
# kworb/spotify_viral gives release year, not viral year.
# TikTok-specific sources give actual viral year.
_YEAR_PRIORITY: dict[str, int] = {
    "tiktok_year": 3,
    "billboard_trending": 2,
    "wiki_viral": 1,
    "spotify_viral": 0,
}

HEADERS = {
    "User-Agent": "songs-sense-bot/1.0 (educational project; contact: oskobx@gmail.com)"
}
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

_FOOTNOTE_RE = re.compile(r"\[.*?\]")
_QUOTE_RE = re.compile(r'^[""""'']+|[""""'']+$')
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_SPED_RE = re.compile(
    r"\b(sped[\s\-]?up|slowed[\s\-]?(down)?|nightcore|reverb[\s\-]?ed|pitched[\s\-]?up)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _clean_cell(text: str) -> str:
    text = _FOOTNOTE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_quotes(text: str) -> str:
    return _QUOTE_RE.sub("", text).strip()


def _find_col(headers: list[str], *keywords: str) -> int | None:
    for i, h in enumerate(headers):
        if any(k in h.lower() for k in keywords):
            return i
    return None


# ---------------------------------------------------------------------------
# Source 1: Wikipedia "TikTok Billboard Top 50"
# Weekly number-one songs, launched September 2023.
# Tables: No. | Issue date | Song | Artist(s) — one table per year section.
# ---------------------------------------------------------------------------


def _parse_tiktok_billboard_top50(
    client: httpx.Client, probe_year: int | None = None
) -> list[dict]:
    """Scrape Wikipedia TikTok Billboard Top 50 number-ones table."""
    url = "https://en.wikipedia.org/wiki/TikTok_Billboard_Top_50"
    try:
        resp = client.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"    WARNING [tiktok_billboard]: HTTP {resp.status_code} — skipping")
            return []
    except httpx.HTTPError as exc:
        print(f"    WARNING [tiktok_billboard]: {exc} — skipping")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    records: list[dict] = []
    current_year: int | None = None

    for el in soup.find_all(["h2", "h3", "h4", "table"]):
        if el.name in ("h2", "h3", "h4"):
            m = _YEAR_RE.search(el.get_text())
            if m:
                y = int(m.group())
                current_year = y if 2019 <= y <= 2026 else None
            continue

        if "wikitable" not in " ".join(el.get("class", [])):
            continue
        if current_year is None:
            continue
        if probe_year is not None and current_year != probe_year:
            continue

        rows = el.find_all("tr")
        if not rows:
            continue

        header_cells = [_clean_cell(c.get_text()) for c in rows[0].find_all(["th", "td"])]
        h = [c.lower() for c in header_cells]

        # Skip milestone / summary tables (first header "Number of weeks" etc.)
        if any(k in h[0] for k in ("number", "weeks", "milestone")):
            continue

        song_col = _find_col(h, "song", "title")
        artist_col = _find_col(h, "artist")
        if song_col is None:
            continue

        seen_in_table: set[str] = set()
        all_data_rows = [r for r in rows[1:] if r.find_all(["td", "th"])]
        list_length = max(len(all_data_rows), 1)

        for rank, row in enumerate(all_data_rows, start=1):
            cells = row.find_all(["td", "th"])
            texts = [_clean_cell(c.get_text(separator=" ")) for c in cells]
            if len(texts) < 2:
                continue

            s_idx = song_col if song_col < len(texts) else 0
            a_idx = (
                artist_col
                if (artist_col is not None and artist_col < len(texts))
                else s_idx + 1
            )

            title = _strip_quotes(texts[s_idx])
            artist_raw = texts[a_idx] if a_idx < len(texts) else ""

            if not title or len(title) < 2 or not artist_raw or len(artist_raw) < 2:
                continue
            if title.lower() in ("song", "title", "artist"):
                continue

            key = normalize_for_dedup(title) + "|||" + normalize_for_dedup(artist_raw)
            if key in seen_in_table:
                continue
            seen_in_table.add(key)

            norm = (list_length + 1 - rank) / list_length
            records.append({
                "artist_raw": artist_raw,
                "title": title,
                "year": current_year,
                "original_year": None,
                "source": "billboard_trending",
                "weight": SOURCE_WEIGHTS["billboard_trending"],
                "norm_score": norm,
            })

    years_found = sorted({r["year"] for r in records})
    print(f"    INFO [tiktok_billboard]: {len(records)} rows (years: {years_found})")
    return records


# ---------------------------------------------------------------------------
# Source 2: TikTok newsroom "Year on TikTok" pages (2023, 2024)
# Songs listed as "Title" - Artist: description text.
# TikTok newsroom 2021/2022 redirect to generic page; 2023+ have real content.
# ---------------------------------------------------------------------------

_NEWSROOM_URLS: dict[int, str] = {
    2023: "https://newsroom.tiktok.com/en-us/year-on-tiktok-2023",
    2024: "https://newsroom.tiktok.com/en-us/year-on-tiktok-2024",
}

_NEWSROOM_SONG_RE = re.compile(
    r'[""""](.{3,80}?)[""""]\s*[-–]\s*([A-Z][^\n:]{2,60}?)(?=\s*[:\n])',
)


def _parse_tiktok_newsroom(client: httpx.Client, year: int) -> list[dict]:
    """Scrape TikTok newsroom Year on TikTok page for a specific year."""
    url = _NEWSROOM_URLS.get(year)
    if url is None:
        return []

    try:
        resp = client.get(url, timeout=30, headers=BROWSER_HEADERS)
        if resp.status_code != 200:
            print(f"    WARNING [tiktok_newsroom {year}]: HTTP {resp.status_code} — skipping")
            return []
        if len(resp.text) < 150_000:
            print(
                f"    WARNING [tiktok_newsroom {year}]: response looks like stub"
                f" ({len(resp.text)} bytes) — skipping"
            )
            return []
    except httpx.HTTPError as exc:
        print(f"    WARNING [tiktok_newsroom {year}]: {exc} — skipping")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(separator="\n", strip=True)

    # Locate the songs / music section
    music_idx = -1
    for keyword in ("Top 10 Songs", "Top Song", "soundtrack", "popular song"):
        idx = text.lower().find(keyword.lower())
        if idx >= 0:
            music_idx = idx
            break

    if music_idx < 0:
        print(f"    WARNING [tiktok_newsroom {year}]: no music section found")
        return []

    window = text[music_idx:]
    matches = _NEWSROOM_SONG_RE.findall(window)

    records: list[dict] = []
    seen: set[str] = set()
    list_length = max(len(matches), 1)

    for rank, (title_raw, artist_raw) in enumerate(matches, start=1):
        title = title_raw.strip()
        artist = artist_raw.strip()
        if not title or not artist or len(title) < 2:
            continue
        key = normalize_for_dedup(title) + "|||" + normalize_for_dedup(artist)
        if key in seen:
            continue
        seen.add(key)

        norm = (list_length + 1 - rank) / list_length
        records.append({
            "artist_raw": artist,
            "title": title,
            "year": year,
            "original_year": None,
            "source": "tiktok_year",
            "weight": SOURCE_WEIGHTS["tiktok_year"],
            "norm_score": norm,
        })

    print(f"    INFO [tiktok_newsroom {year}]: {len(records)} songs extracted")
    return records


# ---------------------------------------------------------------------------
# Source 3: kworb.net per-year Spotify top songs (2019-2025)
# kworb aggregates Spotify chart data by year (songs most streamed in that year).
# Used as a proxy for Spotify viral chart since Spotify API requires premium.
# Format: "Artist - Title | Streams | Daily"
# ---------------------------------------------------------------------------


def _parse_kworb_yearly(
    client: httpx.Client, year: int, limit: int = KWORB_LIMIT_PER_YEAR
) -> list[dict]:
    """Scrape kworb.net top-streamed songs for a given year."""
    url = f"https://kworb.net/spotify/songs_{year}.html"
    try:
        resp = client.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"    WARNING [kworb {year}]: HTTP {resp.status_code} — skipping")
            return []
    except httpx.HTTPError as exc:
        print(f"    WARNING [kworb {year}]: {exc} — skipping")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        print(f"    WARNING [kworb {year}]: no table found")
        return []

    rows = table.find_all("tr")[1 : limit + 1]  # skip header, take up to limit
    records: list[dict] = []

    for rank, row in enumerate(rows, start=1):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        combined = _clean_cell(cells[0].get_text())
        # Format: "Artist - Title"  (first dash separator)
        if " - " not in combined:
            continue
        artist_raw, title = combined.split(" - ", 1)
        artist_raw = artist_raw.strip()
        title = title.strip()
        if not artist_raw or not title or len(title) < 2:
            continue
        # Skip ambient / sleep / white-noise entries
        if any(w in title.lower() for w in ("sleep", "white noise", "rain", "binaural", "asmr")):
            continue

        list_length = min(len(rows), limit)
        norm = (list_length + 1 - rank) / list_length
        records.append({
            "artist_raw": artist_raw,
            "title": title,
            "year": year,
            "original_year": None,
            "source": "spotify_viral",
            "weight": SOURCE_WEIGHTS["spotify_viral"],
            "norm_score": norm,
        })

    print(f"    INFO [kworb {year}]: {len(records)} songs scraped")
    return records


# ---------------------------------------------------------------------------
# Source 4: Wikipedia song article scan (back-catalog viral songs)
# Searches Wikipedia for "(song)" articles that mention TikTok virality,
# then batch-fetches article intros to extract original_year and viral year.
# Captures entries like "Running Up That Hill" (1985, viral 2022).
# ---------------------------------------------------------------------------

_WIKI_INTRO_ARTIST_RE = re.compile(
    r'\b(?:song|single|track|record)\b.*?\bby\b\s+(.*?)(?:[.,]|\s+(?:taken|from|released|which|that|feat|featuring))',
    re.DOTALL | re.IGNORECASE,
)
# Leading nationality/role descriptors to strip from Wikipedia "by ..." phrases
_WIKI_DESCRIPTOR_RE = re.compile(
    r'^(?:(?:the|a|an|and|or)\s+|'
    r'(?:(?:American|British|Canadian|Australian|New\s+Zealand|South\s+Korean|'
    r'Korean(?:[\s\-]American)?|Japanese|Swedish|Norwegian|Danish|Finnish|German|'
    r'French|Italian|Spanish|Irish|Scottish|Welsh|English|Hong\s+Kong|Filipino|'
    r'Indonesian|Brazilian|Colombian|Puerto\s+Rican|Mexican|Nigerian|Jamaican|'
    r'South\s+African)\s+)|'
    r'(?:(?:singer|rapper|musician|music\s+artist|artist|songwriter|producer|'
    r'group|girl\s+group|boy\s+group|band|duo|trio|'
    r'DJ|MC|recording\s+artist|alternative|rock|pop|country|hip[\s\-]hop|'
    r'R&B|electronic|indie|folk|metal|jazz|singer[\s\-]songwriter|house|'
    r'multi[\s\-]talented|soft|hard)\s+)|'
    r'(?:and\s+singer\s+))+',
    re.IGNORECASE,
)
_WIKI_TIKTOK_YEAR_RE = re.compile(
    r'(?:went\s+)?viral\s+on\s+TikTok(?:\s+in\s+(\d{4}))?',
    re.IGNORECASE,
)
_WIKI_RELEASE_YEAR_RE = re.compile(
    r'\breleased(?:\s+as\s+a\s+single)?\s+(?:on\s+)?(?:[A-Za-z]+\s+\d{1,2},\s*)?(\d{4})\b'
    r'|\bfirst\s+(?:released|appeared|published)\s+in\s+(\d{4})\b'
    r'|\bin\s+(\d{4})\b.*?\breleased\b',
    re.IGNORECASE,
)


def _extract_artist_from_intro(intro: str) -> str | None:
    """Extract the primary artist name from a Wikipedia song article intro."""
    m = _WIKI_INTRO_ARTIST_RE.search(intro)
    if not m:
        return None
    raw = m.group(1).strip()
    # Strip nationality/role descriptor prefixes ("American singer", "the British band", etc.)
    raw = _WIKI_DESCRIPTOR_RE.sub("", raw).strip()
    # Strip any remaining leading articles
    raw = re.sub(r"^(?:the|a|an)\s+", "", raw, flags=re.IGNORECASE)
    raw = raw.strip()
    return raw if len(raw) >= 2 else None


def _extract_release_year(intro: str) -> int | None:
    """Extract the original release year from article intro."""
    m = _WIKI_RELEASE_YEAR_RE.search(intro)
    if m:
        for g in m.groups():
            if g:
                return int(g)
    # Fallback: first 4-digit year in the intro that looks like a release year
    years = [int(y) for y in _YEAR_RE.findall(intro[:400]) if 1950 <= int(y) <= 2026]
    return years[0] if years else None


def _extract_tiktok_viral_year(intro: str) -> int | None:
    """Extract the year the song went viral on TikTok from article intro."""
    m = _WIKI_TIKTOK_YEAR_RE.search(intro)
    if not m:
        return None
    if m.group(1):
        y = int(m.group(1))
        return y if 2019 <= y <= 2026 else None
    # No explicit year — look in context window
    ctx_start = max(0, m.start() - 200)
    ctx = intro[ctx_start : m.end() + 200]
    candidates = [
        int(y) for y in _YEAR_RE.findall(ctx) if 2019 <= int(y) <= 2026
    ]
    return candidates[0] if candidates else None


def _parse_wiki_tiktok_articles(client: httpx.Client) -> list[dict]:
    """Search Wikipedia for song articles mentioning TikTok virality, then parse them."""
    # Step 1: gather article titles via paginated search
    article_titles: list[str] = []
    for query in [
        "TikTok viral song",
        "went viral TikTok song",
        "TikTok trend song",
        "went viral on TikTok single",
    ]:
        for offset in range(0, 200, 50):
            try:
                r = client.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "list": "search",
                        "srsearch": query,
                        "srlimit": 50,
                        "sroffset": offset,
                        "format": "json",
                        "srnamespace": 0,
                    },
                    timeout=15,
                )
                results = r.json().get("query", {}).get("search", [])
                if not results:
                    break
                for item in results:
                    t = item["title"]
                    if re.search(r"\((?:\w+ )?(?:song|single|EP)\)", t, re.IGNORECASE):
                        article_titles.append(t)
            except Exception:
                pass
            time.sleep(0.3)

    article_titles = list(dict.fromkeys(article_titles))
    print(f"    INFO [wiki_articles]: {len(article_titles)} unique (song) article candidates")

    if not article_titles:
        return []

    # Step 2: batch-fetch article intros (extracts)
    records: list[dict] = []
    batch_size = 20

    for i in range(0, len(article_titles), batch_size):
        batch = article_titles[i : i + batch_size]
        try:
            r = client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "titles": "|".join(batch),
                    "prop": "extracts",
                    "exintro": True,
                    "explaintext": True,
                    "format": "json",
                },
                timeout=20,
            )
            pages = r.json().get("query", {}).get("pages", {})
        except Exception:
            continue

        for page in pages.values():
            wiki_title = page.get("title", "")
            intro = page.get("extract", "")
            if not intro:
                continue

            # Check if article actually mentions TikTok
            if "tiktok" not in intro.lower():
                continue

            # Extract viral year (must be 2019-2026)
            viral_year = _extract_tiktok_viral_year(intro)
            if viral_year is None:
                continue

            # Extract song name from article title
            song_name = re.sub(
                r"\s*\((?:\w+ )?(?:song|single|EP)[^)]*\)\s*$",
                "",
                wiki_title,
                flags=re.IGNORECASE,
            ).strip()
            if len(song_name) < 2:
                continue

            # Extract artist from article text
            artist_raw = _extract_artist_from_intro(intro)
            if not artist_raw or len(artist_raw) < 2:
                continue

            # Extract original release year
            release_year = _extract_release_year(intro)
            original_year: int | None = None
            if release_year and release_year != viral_year:
                original_year = release_year

            records.append({
                "artist_raw": artist_raw,
                "title": song_name,
                "year": viral_year,
                "original_year": original_year,
                "source": "wiki_viral",
                "weight": SOURCE_WEIGHTS["wiki_viral"],
                "norm_score": 0.5,  # no rank signal from article list
            })

        time.sleep(0.3)

    print(f"    INFO [wiki_articles]: {len(records)} songs extracted with TikTok viral year")
    return records


# ---------------------------------------------------------------------------
# Collect all sources
# ---------------------------------------------------------------------------


def _collect_all(client: httpx.Client) -> list[dict]:
    all_records: list[dict] = []

    print("\n[tiktok_billboard] Wikipedia TikTok Billboard Top 50 number-ones...")
    all_records += _parse_tiktok_billboard_top50(client)
    time.sleep(0.5)

    print("\n[tiktok_newsroom] TikTok newsroom Year on TikTok (2023, 2024)...")
    for year in [2023, 2024]:
        all_records += _parse_tiktok_newsroom(client, year)
        time.sleep(0.5)

    print("\n[kworb] kworb.net per-year Spotify top songs (2019-2025)...")
    for year in range(2019, 2026):
        all_records += _parse_kworb_yearly(client, year)
        time.sleep(0.4)

    print("\n[wiki_articles] Wikipedia song article scan (back-catalog viral songs)...")
    all_records += _parse_wiki_tiktok_articles(client)

    return all_records


# ---------------------------------------------------------------------------
# Scoring and union
# ---------------------------------------------------------------------------


def _score_and_union(records: list[dict]) -> pd.DataFrame:
    """Merge duplicate songs within Tier 5, compute combined score."""
    candidates: dict[str, dict] = {}

    for rec in records:
        artist_str = str(rec.get("artist_raw") or "").strip()
        title_str = str(rec.get("title") or "").strip()
        if not artist_str or not title_str:
            continue

        artist, featured = parse_featured_artists(artist_str)
        a_key = normalize_for_dedup(artist)
        t_key = normalize_for_dedup(title_str)
        if not a_key or not t_key:
            continue
        dedup_key = f"{a_key}|||{t_key}"

        source = rec.get("source", "wiki_viral")
        weight = float(rec.get("weight", 0.5))
        norm = float(rec.get("norm_score", 0.5))
        year = rec.get("year")
        original_year = rec.get("original_year")
        weighted = norm * weight

        year_priority = _YEAR_PRIORITY.get(source, 0)

        if dedup_key not in candidates:
            candidates[dedup_key] = {
                "_key": dedup_key,
                "_artist_key": a_key,
                "_title_key": t_key,
                "artist": artist,
                "featured_artists": featured,
                "title": title_str,
                "year": year,
                "original_year": original_year,
                "combined_score": 0.0,
                "sources": [],
                "_best_year_priority": year_priority,
            }
        else:
            existing = candidates[dedup_key]
            if featured and not existing["featured_artists"]:
                existing["featured_artists"] = featured
            if original_year and not existing["original_year"]:
                existing["original_year"] = original_year
            # Higher-priority source wins on year assignment
            # (tiktok_year/billboard_trending > wiki_viral > spotify_viral)
            if year and year_priority > existing["_best_year_priority"]:
                existing["year"] = year
                existing["_best_year_priority"] = year_priority

        candidates[dedup_key]["combined_score"] += weighted
        if source not in candidates[dedup_key]["sources"]:
            candidates[dedup_key]["sources"].append(source)

    if not candidates:
        return pd.DataFrame()

    df = pd.DataFrame(candidates.values())
    df["source"] = df["sources"].apply(lambda s: ", ".join(s))
    df = df.drop(columns=["sources", "_best_year_priority"])
    # original_year == year means no meaningful back-catalog signal — clear it
    df.loc[df["original_year"] == df["year"], "original_year"] = None
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Sped-up / slowed version filter
# ---------------------------------------------------------------------------


def _drop_sped_slowed(df: pd.DataFrame) -> pd.DataFrame:
    """Drop sped-up/slowed variants when the studio original is present."""
    is_variant = df["title"].apply(lambda t: bool(_SPED_RE.search(t)))
    base_keys: set[str] = set(df.loc[~is_variant, "_key"])
    to_drop: list = []
    for idx, row in df[is_variant].iterrows():
        stripped = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", row["title"]).strip()
        base_candidate = row["_artist_key"] + "|||" + normalize_for_dedup(stripped)
        if base_candidate in base_keys:
            to_drop.append(idx)
    if to_drop:
        print(f"    Dropped {len(to_drop)} sped-up/slowed variants")
    return df.drop(index=to_drop)


# ---------------------------------------------------------------------------
# Load prior tier keys for dedup
# ---------------------------------------------------------------------------


def _load_tier_keys(path: Path) -> set[str]:
    if not path.exists():
        print(f"  WARNING: {path} not found — skipping dedup against it")
        return set()
    t = pd.read_csv(path)
    return {
        normalize_for_dedup(str(r["artist"])) + "|||" + normalize_for_dedup(str(r["title"]))
        for _, r in t.iterrows()
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    probe = "--probe" in sys.argv

    if probe:
        print("=== Tier 5 PROBE: Wikipedia TikTok Billboard Top 50 (2023) ===\n")
        with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
            records = _parse_tiktok_billboard_top50(client, probe_year=2023)

        if not records:
            print("\nNo records scraped — check the URL or table structure.")
            sys.exit(1)

        df = pd.DataFrame(records)
        print(f"\nTotal rows scraped for 2023: {len(df)}")
        print("\nFirst 20 rows:")
        print(df[["artist_raw", "title", "year"]].head(20).to_string(index=True))
        print(
            "\nVerify these are real viral songs, then re-run without --probe for the full build."
        )
        return

    # ── Full run ────────────────────────────────────────────────────────────
    print("=== Tier 5: Viral / TikTok Era ===")
    print("Sources: TikTok Billboard Top 50, newsroom 2023/2024, kworb yearly, Wikipedia articles\n")

    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        raw = _collect_all(client)

    print(f"\nTotal raw records collected: {len(raw)}")

    raw_df = pd.DataFrame(raw)
    print("\nPer-year raw counts:")
    for y in VIRAL_YEARS:
        n = int((raw_df["year"] == y).sum()) if "year" in raw_df.columns else 0
        print(f"  {y}: {n}")
    print("\nPer-source raw counts:")
    for src in SOURCE_WEIGHTS:
        n = int((raw_df["source"] == src).sum()) if "source" in raw_df.columns else 0
        print(f"  {src}: {n}")

    # ── Score and union ───────────────────────────────────────────────────
    df = _score_and_union(raw)
    if df.empty:
        print("ERROR: no candidates after scoring.")
        sys.exit(1)

    print(f"\nAfter within-Tier-5 union (one row per song): {len(df)}")

    df = _drop_sped_slowed(df)
    print(f"After sped-up/slowed filter: {len(df)}")

    # Across years within Tier 5: keep higher-score year for the same song
    before = len(df)
    df = df.sort_values("combined_score", ascending=False).drop_duplicates(
        subset=["_artist_key", "_title_key"], keep="first"
    )
    print(f"After cross-year dedup: {len(df)} (removed {before - len(df)})")

    # ── Dedup against prior tiers ─────────────────────────────────────────
    print("\nDeduplicating against prior tiers…")
    t1_keys = _load_tier_keys(TIER1_PATH)
    t2_keys = _load_tier_keys(TIER2_PATH)
    t3_keys = _load_tier_keys(TIER3_PATH)
    t4_keys = _load_tier_keys(TIER4_PATH)

    in_t1 = df["_key"].isin(t1_keys)
    n_t1 = int(in_t1.sum())
    df = df[~in_t1].copy()
    print(f"  Removed {n_t1} rows matching Tier 1")

    in_t2 = df["_key"].isin(t2_keys)
    n_t2 = int(in_t2.sum())
    df = df[~in_t2].copy()
    print(f"  Removed {n_t2} rows matching Tier 2")

    in_t3 = df["_key"].isin(t3_keys)
    n_t3 = int(in_t3.sum())
    df = df[~in_t3].copy()
    print(f"  Removed {n_t3} rows matching Tier 3")

    in_t4 = df["_key"].isin(t4_keys)
    n_t4 = int(in_t4.sum())
    df = df[~in_t4].copy()
    print(f"  Removed {n_t4} rows matching Tier 4")

    # ── Finalise ──────────────────────────────────────────────────────────
    df = df.rename(columns={"combined_score": "source_rank"})
    df["tier"] = "viral"
    final = df[
        [
            "artist", "featured_artists", "title",
            "year", "original_year",
            "source", "source_rank", "tier",
        ]
    ].reset_index(drop=True)

    # Final dedup safety-net: normalize-based, keeps highest source_rank row
    final["_ak"] = final["artist"].apply(normalize_for_dedup)
    final["_tk"] = final["title"].apply(normalize_for_dedup)
    pre_final_dedup = len(final)
    final = (
        final.sort_values("source_rank", ascending=False)
        .drop_duplicates(subset=["_ak", "_tk"], keep="first")
        .drop(columns=["_ak", "_tk"])
        .reset_index(drop=True)
    )
    if len(final) < pre_final_dedup:
        print(f"  Final dedup safety-net removed {pre_final_dedup - len(final)} rows")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    final.to_csv(OUTPUT_PATH, index=False)

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n=== Summary ===")
    print(f"  Raw records collected:          {len(raw)}")
    print(f"  Removed (Tier 1 overlap):       {n_t1}")
    print(f"  Removed (Tier 2 overlap):       {n_t2}")
    print(f"  Removed (Tier 3 overlap):       {n_t3}")
    print(f"  Removed (Tier 4 overlap):       {n_t4}")
    print(f"  Total final row count:          {len(final)}")

    feat = int(final["featured_artists"].notna().sum())
    back_catalog = int(
        (
            final["original_year"].notna()
            & ((final["year"] - final["original_year"]) > 3)
        ).sum()
    )
    print(f"  Rows with featured artists:     {feat}")
    print(f"  Back-catalog virality rows:     {back_catalog}")

    print("\nPer-year final counts:")
    for y in VIRAL_YEARS:
        n = int((final["year"] == y).sum())
        print(f"  {y}: {n}")
    print(f"\nOutput: {OUTPUT_PATH}")

    # ── Success criteria ──────────────────────────────────────────────────
    print("\n=== Success Criteria ===")
    all_ok = True

    count_ok = 400 <= len(final) <= 600
    print(f"  {'OK' if count_ok else 'FAIL'} Total rows {len(final)} (target 400-600)")
    all_ok = all_ok and count_ok

    no_dupes = not final.duplicated(subset=["artist", "title"]).any()
    print(f"  {'OK' if no_dupes else 'FAIL'} No exact (artist, title) duplicates")
    all_ok = all_ok and no_dupes

    years_ge50 = sum(1 for y in VIRAL_YEARS if int((final["year"] == y).sum()) >= 50)
    yr_ok = years_ge50 >= 4
    print(f"  {'OK' if yr_ok else 'FAIL'} {years_ge50}/8 years have ≥50 rows (need ≥4)")
    all_ok = all_ok and yr_ok

    bc_ok = back_catalog >= 20
    print(
        f"  {'OK' if bc_ok else 'FAIL'} {back_catalog} back-catalog virality rows (need ≥20)"
    )
    all_ok = all_ok and bc_ok

    final_keys = (
        final["artist"].apply(normalize_for_dedup)
        + "|||"
        + final["title"].apply(normalize_for_dedup)
    )
    for tier_name, tier_keys in [
        ("Tier 1", t1_keys), ("Tier 2", t2_keys),
        ("Tier 3", t3_keys), ("Tier 4", t4_keys),
    ]:
        clean = not final_keys.isin(tier_keys).any()
        print(f"  {'OK' if clean else 'FAIL'} No {tier_name} songs in Tier 5")
        all_ok = all_ok and clean

    if not all_ok:
        print("\nSome criteria not met — review per-year counts and sources above.")
        sys.exit(1)
    else:
        print("\nAll success criteria met.")


if __name__ == "__main__":
    main()
