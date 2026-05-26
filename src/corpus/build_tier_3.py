"""Build data/tier_3_recent.csv from multiple chart sources (2015-2026).

Mandatory: Billboard Year-End Hot 100, six Billboard genre charts,
           Spotify Global (API), Spotify UK (API).
Bonus:     Spotify US, Apple Music, YouTube Music — skip on failure.

Run from the project root:
    uv run python -m src.corpus.build_tier_3
"""

from __future__ import annotations

import base64
import os
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUTPUT_PATH = Path("data/tier_3_recent.csv")
TIER1_PATH = Path("data/tier_1_canonical.csv")
TIER2_PATH = Path("data/tier_2_decades.csv")

YEARS = list(range(2015, 2027))
TOP_N_PER_YEAR = 420
PARTIAL_YEAR = 2026  # year-end data not yet published

SOURCE_WEIGHTS: dict[str, float] = {
    "billboard_hot_100": 1.0,
    "billboard_rap": 1.0,
    "billboard_country": 1.0,
    "billboard_latin": 1.0,
    "billboard_rnb": 1.0,
    "billboard_dance": 1.0,
    "billboard_rock": 1.0,
    "spotify_global": 0.5,
    "spotify_uk": 0.5,
    "spotify_us": 0.4,
    "apple_music": 0.4,
    "youtube_music": 0.4,
}

# Multiple Wikipedia slug candidates per genre chart (tried in order)
_GENRE_WIKI_SLUGS: dict[str, list[str]] = {
    "billboard_rap": [
        "Billboard_Year-End_Hot_Rap_Songs_of_{year}",
    ],
    "billboard_country": [
        "Billboard_Year-End_Hot_Country_Songs_of_{year}",
    ],
    "billboard_latin": [
        "Billboard_Year-End_Hot_Latin_Songs_of_{year}",
    ],
    "billboard_rnb": [
        "Billboard_Year-End_Hot_R%26B%2FHip-Hop_Songs_of_{year}",
        "Billboard_Year-End_Hot_R%26B_Hip-Hop_Songs_of_{year}",
    ],
    "billboard_dance": [
        "Billboard_Year-End_Hot_Dance%2FElectronic_Songs_of_{year}",
        "Billboard_Year-End_Hot_Dance_and_Electronic_Songs_of_{year}",
    ],
    "billboard_rock": [
        "Billboard_Year-End_Hot_Rock_%26_Alternative_Songs_of_{year}",
        "Billboard_Year-End_Hot_Rock_Songs_of_{year}",
        "Billboard_Year-End_Hot_Rock_and_Alternative_Songs_of_{year}",
    ],
}

# Per-year "number-one songs of YYYY" Wikipedia pages.
# Used as fallback when year-end genre charts don't exist (all Billboard genre year-end
# pages for 2018+ are absent from Wikipedia). Each page lists weekly chart-toppers for
# the year; we extract all unique songs across both the main chart and airplay columns.
_GENRE_NUMBERONE_SLUGS: dict[str, list[str]] = {
    "billboard_country": [
        "List_of_Billboard_number-one_country_songs_of_{year}",
    ],
    "billboard_latin": [
        "List_of_Billboard_number-one_Latin_songs_of_{year}",
    ],
    "billboard_dance": [
        "List_of_Billboard_number-one_dance_songs_of_{year}",
    ],
    "billboard_rnb": [
        "List_of_number-one_R%26B%2Fhip-hop_songs_of_{year}_(U.S.)",
        "List_of_Billboard_Hot_R%26B%2FHip-Hop_Songs_number_ones_of_{year}",
    ],
    # Rock: no reliable Wikipedia number-one page exists.
    "billboard_rock": [],
    # Rap year-end slug handles 2015-2017; R&B number-one page covers rap for 2018+.
    "billboard_rap": [],
}

_FOOTNOTE_RE = re.compile(r"\[.*?\]")
_QUOTE_RE = re.compile('^[\u201c\u201d"]+|[\u201c\u201d"]+$')
_VERSION_RE = re.compile(
    r"\b(live|remix|acoustic|demo|instrumental|reprise|radio\s+edit)\b",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": "songs-sense-bot/1.0 (educational project; contact: oskobx@gmail.com)"
}

# ---------------------------------------------------------------------------
# Shared HTML parsing utilities (same patterns as build_tier_2)
# ---------------------------------------------------------------------------


def _clean_cell(text: str) -> str:
    text = _FOOTNOTE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _QUOTE_RE.sub("", text).strip()
    return text


def _find_col_indices(header_row) -> tuple[int, int, int] | None:
    cells = header_row.find_all(["th", "td"])
    headers = [c.get_text(strip=True).lower() for c in cells]
    rank_idx = title_idx = artist_idx = None
    for i, h in enumerate(headers):
        if rank_idx is None and h in ("no.", "no", "#", "rank", "pos", "pos."):
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


def _parse_wiki_table_rows(
    rows: list, rank_idx: int, title_idx: int, artist_idx: int, year: int
) -> list[dict]:
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
        records.append(
            {"rank": rank, "title": title_text, "artist_raw": artist_text, "year": year}
        )
    return records


def _try_wiki_table(table, year: int, min_rows: int = 15) -> list[dict]:
    rows = table.find_all("tr")
    if not rows:
        return []
    col_indices = _find_col_indices(rows[0])
    if col_indices is None and len(rows) > 1:
        col_indices = _find_col_indices(rows[1])
    if col_indices is not None:
        records = _parse_wiki_table_rows(rows, *col_indices, year)
        if len(records) >= min_rows:
            return records
    # Positional fallback (standard Billboard Wikipedia layout)
    records = _parse_wiki_table_rows(rows, 0, 1, 2, year)
    if len(records) >= min_rows:
        return records
    return []


# ---------------------------------------------------------------------------
# Billboard scraping
# ---------------------------------------------------------------------------


def _parse_numberone_row_cells(cells: list) -> list[tuple[str, str]]:
    """Extract (title, artist) pairs from table row cells.

    Handles two Wikipedia table formats used across genre number-one pages:
    - Combined: "Song Title\\nArtist Name" in a single cell (Country format)
    - Separate: quoted title cell followed by artist cell (R&B/Latin/Dance format)
    """
    results: list[tuple[str, str]] = []
    pending_title: str | None = None

    for i, cell in enumerate(cells):
        if i == 0:
            continue  # first column is always the issue date

        raw = _FOOTNOTE_RE.sub("", cell.get_text(separator="\n"))
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]

        if not lines:
            pending_title = None
            continue

        first_line = lines[0]

        # Skip reference cells: "[1]", "[2]", bare digits
        if re.match(r"^\[?\d+\]?$", first_line.strip()):
            pending_title = None
            continue

        # Skip chart-continuation placeholders
        if first_line.strip() in ("–", "—", "-", "——"):
            pending_title = None
            continue

        if len(lines) >= 2:
            # Combined cell: title on first line, artist on second
            title = _QUOTE_RE.sub("", first_line).strip()
            artist = lines[1].strip()
            if title and artist and len(title) >= 2 and len(artist) >= 2:
                results.append((title, artist))
            pending_title = None
        else:
            # Single-line cell: distinguish title vs. artist by leading/trailing quotes
            is_quoted = bool(
                re.match(r'^[“”"]', first_line)
                or re.search(r'[“”"]$', first_line)
            )
            if is_quoted:
                pending_title = _QUOTE_RE.sub("", first_line).strip()
            elif pending_title is not None:
                # Unquoted cell following a title → artist name
                artist = re.sub(r"\s+", " ", first_line).strip()
                if artist and len(artist) >= 2:
                    results.append((pending_title, artist))
                pending_title = None
            # else: unquoted, no pending title (probably a date or stray text) → skip

    return results


def _parse_billboard_genre_numberones(
    source_key: str, year: int, client: httpx.Client
) -> list[dict]:
    """Fallback: parse a Billboard genre 'number-one songs of YYYY' Wikipedia page.

    Used when the year-end genre chart page doesn't exist (2018+ for most genres).
    Extracts all unique songs that hit #1 on any column of the table, ranks by
    number of week-rows they appear in (proxy for weeks at #1).
    """
    slugs = _GENRE_NUMBERONE_SLUGS.get(source_key, [])
    if not slugs:
        return []

    for slug_template in slugs:
        slug = slug_template.format(year=year)
        url = f"https://en.wikipedia.org/wiki/{slug}"
        try:
            resp = client.get(url, timeout=30)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
        except httpx.HTTPError:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        song_counts: dict[str, dict] = {}

        for table in soup.find_all("table", class_=re.compile(r"wikitable")):
            table_rows = table.find_all("tr")
            if len(table_rows) < 10:
                continue  # too small to be the main chart table

            for tr in table_rows:
                if not tr.find("td"):
                    continue  # header row
                cells = tr.find_all(["td"])
                if len(cells) < 2:
                    continue

                for title, artist_raw in _parse_numberone_row_cells(cells):
                    a_key = normalize_for_dedup(artist_raw)
                    t_key = normalize_for_dedup(title)
                    key = f"{a_key}|||{t_key}"
                    if key not in song_counts:
                        song_counts[key] = {
                            "title": title,
                            "artist_raw": artist_raw,
                            "count": 0,
                        }
                    song_counts[key]["count"] += 1

        if len(song_counts) >= 5:
            sorted_songs = sorted(
                song_counts.values(), key=lambda x: x["count"], reverse=True
            )
            records = [
                {
                    "rank": rank + 1,
                    "title": s["title"],
                    "artist_raw": s["artist_raw"],
                    "year": year,
                }
                for rank, s in enumerate(sorted_songs)
            ]
            print(
                f"    INFO [{source_key} {year}]: {len(records)} songs from number-one page"
            )
            return records

        time.sleep(0.3)

    return []


def _parse_billboard_hot100(year: int, client: httpx.Client) -> list[dict]:
    url = f"https://en.wikipedia.org/wiki/Billboard_Year-End_Hot_100_singles_of_{year}"
    try:
        resp = client.get(url, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"    WARNING [billboard_hot_100 {year}]: HTTP error: {exc}")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    for table in soup.find_all("table", class_=re.compile(r"wikitable")):
        records = _try_wiki_table(table, year)
        if records:
            return records
    for table in soup.find_all("table"):
        records = _try_wiki_table(table, year)
        if records:
            return records
    print(f"    WARNING [billboard_hot_100 {year}]: Could not parse table")
    return []


def _parse_billboard_genre(
    source_key: str, year: int, client: httpx.Client
) -> list[dict]:
    # Try year-end chart page first (exists for Rap 2015-2017; others mostly absent)
    for slug_template in _GENRE_WIKI_SLUGS.get(source_key, []):
        slug = slug_template.format(year=year)
        url = f"https://en.wikipedia.org/wiki/{slug}"
        try:
            resp = client.get(url, timeout=30)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
        except httpx.HTTPError:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for table in soup.find_all("table", class_=re.compile(r"wikitable")):
            records = _try_wiki_table(table, year, min_rows=10)
            if records:
                return records
        for table in soup.find_all("table"):
            records = _try_wiki_table(table, year, min_rows=10)
            if records:
                return records
        time.sleep(0.3)
    # Fallback: weekly number-one songs page (exists for Country/Latin/Dance/R&B all years)
    return _parse_billboard_genre_numberones(source_key, year, client)


# ---------------------------------------------------------------------------
# Spotify API (client credentials — accesses public playlists)
# ---------------------------------------------------------------------------

# Official Spotify year-end "Top Songs" playlist IDs.
# These are Spotify-curated editorial playlists published each year.
# Keys are (year, region) tuples; region is "global" or "uk".
# Fallback: dynamic search via API if not hardcoded.
_SPOTIFY_YEAREND_PLAYLISTS: dict[tuple[int, str], str] = {
    # Global - "Top Songs - YYYY" playlists (owner: spotify)
    (2015, "global"): "37i9dQZF1DX9ukdrXQLJGZ",
    (2016, "global"): "37i9dQZF1DXadBuRLNPCQr",
    (2017, "global"): "37i9dQZF1DWYBO1MoTDhZI",
    (2018, "global"): "37i9dQZF1DXe2bobNYDtW8",
    (2019, "global"): "37i9dQZF1DWVRSukIED0e9",
    (2020, "global"): "37i9dQZF1DX7Jl5KP2eZaS",
    (2021, "global"): "37i9dQZF1DXigPI4IPAbyt",
    (2022, "global"): "37i9dQZF1DXbJMiQ53rTyJ",
    (2023, "global"): "37i9dQZF1DX1HCSiPnfutn",
    (2024, "global"): "37i9dQZF1DX6ujZpAN0v9r",
    # UK - "Top Songs - YYYY (UK)" playlists
    (2015, "uk"): "37i9dQZF1DX5q67ZpuRff7",
    (
        2016,
        "uk",
    ): "37i9dQZF1DX5q67ZpuRff7",  # may be same playlist; search fallback used
    (2017, "uk"): "37i9dQZF1DX4EnUZFeoFCm",
    (2018, "uk"): "37i9dQZF1DXdwmD5Q7Gxoh",
    (2019, "uk"): "37i9dQZF1DWZq91oLsHZvy",
    (2020, "uk"): "37i9dQZF1DXaXDsfqi6O6p",
    (2021, "uk"): "37i9dQZF1DXa9wYhFCPgJf",
    (2022, "uk"): "37i9dQZF1DX3LyU0mhfqgP",
    (2023, "uk"): "37i9dQZF1DXbz7Utp0DSTA",
    (2024, "uk"): "37i9dQZF1DX2A29LI7xHn1",
}


def _spotify_token() -> str | None:
    """Get a Spotify API access token using client credentials flow."""
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print(
            "    WARNING [Spotify]: SPOTIFY_CLIENT_ID/SECRET not set — skipping Spotify sources"
        )
        return None
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        resp = httpx.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {credentials}"},
            data={"grant_type": "client_credentials"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except Exception as exc:
        print(f"    WARNING [Spotify]: Could not get token: {exc}")
        return None


def _spotify_search_yearend_playlist(year: int, region: str, token: str) -> str | None:
    """Search Spotify API for the official year-end top-songs playlist."""
    queries = [
        f"Top Songs - {year}" + (" UK" if region == "uk" else ""),
        f"Wrapped Top Songs {year}" + (" UK" if region == "uk" else ""),
        f"Top Songs {year}" + (" UK" if region == "uk" else ""),
    ]
    for q in queries:
        try:
            resp = httpx.get(
                "https://api.spotify.com/v1/search",
                params={"q": q, "type": "playlist", "limit": 20},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            resp.raise_for_status()
            items = resp.json().get("playlists", {}).get("items", []) or []
            for pl in items:
                if not pl:
                    continue
                owner_id = (pl.get("owner") or {}).get("id", "")
                name = pl.get("name", "")
                year_str = str(year)
                if owner_id != "spotify" or year_str not in name:
                    continue
                name_lower = name.lower()
                if region == "uk" and "uk" not in name_lower:
                    continue
                if region == "global" and "uk" in name_lower:
                    continue
                return pl["id"]
        except Exception:
            continue
    return None


def _spotify_playlist_tracks(
    playlist_id: str, year: int, source_key: str, token: str
) -> list[dict]:
    """Fetch all tracks from a Spotify playlist."""
    records: list[dict] = []
    url: str | None = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
    rank = 0
    while url:
        try:
            resp = httpx.get(
                url,
                params={"fields": "next,items(track(name,artists))", "limit": 100},
                headers={"Authorization": f"Bearer {token}"},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            print(f"    WARNING [{source_key} {year}]: Error fetching playlist: {exc}")
            break
        for item in data.get("items", []) or []:
            track = (item or {}).get("track")
            if not track:
                continue
            artists = track.get("artists") or []
            if not artists:
                continue
            rank += 1
            primary = artists[0].get("name", "")
            feat_list = [a.get("name", "") for a in artists[1:] if a.get("name")]
            artist_str = primary + (
                " feat. " + ", ".join(feat_list) if feat_list else ""
            )
            title = track.get("name", "")
            if artist_str and title:
                records.append(
                    {
                        "rank": rank,
                        "title": title,
                        "artist_raw": artist_str,
                        "year": year,
                    }
                )
        url = data.get("next")
        if url:
            time.sleep(0.1)
    return records


def _parse_spotify_source(
    source_key: str, year: int, region: str, token: str | None
) -> list[dict]:
    """Get Spotify year-end tracks for a region using the API."""
    if not token:
        return []
    # Try hardcoded playlist ID first, then dynamic search
    playlist_id = _SPOTIFY_YEAREND_PLAYLISTS.get((year, region))
    if not playlist_id:
        playlist_id = _spotify_search_yearend_playlist(year, region, token)
    if not playlist_id:
        print(
            f"    INFO [{source_key} {year}]: No {region} year-end playlist found — skipping"
        )
        return []
    records = _spotify_playlist_tracks(playlist_id, year, source_key, token)
    return records


# ---------------------------------------------------------------------------
# Spotify Wikipedia fallbacks (used when API credentials are absent)
# ---------------------------------------------------------------------------

_SPOTIFY_ALLTIME_CACHE: list[dict] | None = None
_SPOTIFY_ALLTIME_URL = (
    "https://en.wikipedia.org/wiki/List_of_most-streamed_songs_on_Spotify"
)


def _fetch_spotify_alltime(client: httpx.Client) -> list[dict]:
    """Fetch Wikipedia's all-time top 100 most-streamed Spotify songs (cached)."""
    global _SPOTIFY_ALLTIME_CACHE
    if _SPOTIFY_ALLTIME_CACHE is not None:
        return _SPOTIFY_ALLTIME_CACHE
    try:
        resp = client.get(_SPOTIFY_ALLTIME_URL, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"    WARNING [spotify_wiki]: HTTP error fetching all-time list: {exc}")
        _SPOTIFY_ALLTIME_CACHE = []
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        _SPOTIFY_ALLTIME_CACHE = []
        return []
    # Table 0: Rank | Song | Artist(s) | Streams | Release date
    rows = tables[0].find_all("tr")
    records: list[dict] = []
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) < 5:
            continue
        rank_text = _clean_cell(cells[0].get_text(separator=" "))
        title_text = _clean_cell(cells[1].get_text(separator=" "))
        artist_text = _clean_cell(cells[2].get_text(separator=" "))
        release_text = _clean_cell(cells[4].get_text(separator=" "))
        try:
            rank = int(rank_text)
        except ValueError:
            continue
        yr_m = re.search(r"\b(20\d{2})\b", release_text)
        if not yr_m:
            continue
        yr = int(yr_m.group(1))
        if title_text and artist_text:
            records.append(
                {
                    "rank": rank,
                    "title": title_text,
                    "artist_raw": artist_text,
                    "year": yr,
                }
            )
    _SPOTIFY_ALLTIME_CACHE = records
    return records


def _parse_spotify_global_wiki(year: int, client: httpx.Client) -> list[dict]:
    """Wikipedia fallback: filter all-time top 100 by release year, re-rank 1..N."""
    all_time = _fetch_spotify_alltime(client)
    year_songs = sorted(
        [r for r in all_time if r["year"] == year], key=lambda r: r["rank"]
    )
    result = [dict(r, rank=i + 1) for i, r in enumerate(year_songs)]
    if result:
        print(
            f"    INFO [spotify_global_wiki {year}]: {len(result)} songs from all-time list"
        )
    return result


def _parse_uk_top10_wiki(year: int, client: httpx.Client) -> list[dict]:
    """Wikipedia UK top-ten singles as Spotify UK proxy.

    Parses songs that entered the UK top 10 in a given year; uses peak position
    (1-10) as rank for normalized scoring.
    """
    url = f"https://en.wikipedia.org/wiki/List_of_UK_top-ten_singles_in_{year}"
    try:
        resp = client.get(url, timeout=30)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"    WARNING [spotify_uk_wiki {year}]: HTTP error: {exc}")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    # Find wikitable with 7 data columns (date|weeks|single|artist|peak|peak date|weeks@peak)
    target: object | None = None
    for table in soup.find_all("table", class_="wikitable"):
        data_rows = [
            r for r in table.find_all("tr") if len(r.find_all(["td", "th"])) >= 6
        ]
        if len(data_rows) >= 30:
            target = table
            break
    if target is None:
        print(f"    INFO [spotify_uk_wiki {year}]: No suitable table")
        return []
    rows = target.find_all("tr")
    records: list[dict] = []
    seen: set[str] = set()
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        n = len(cells)
        if n < 5:
            continue
        cell_texts = [_clean_cell(c.get_text(separator=" ")) for c in cells]
        # Get raw text for title column before _clean_cell strips leading quotes
        raw_texts = [
            _FOOTNOTE_RE.sub("", c.get_text(separator=" ")).strip() for c in cells
        ]
        if n == 7:
            raw_title, artist_text, peak_text = (
                raw_texts[2],
                cell_texts[3],
                cell_texts[4],
            )
        elif n == 6:
            raw_title, artist_text, peak_text = (
                raw_texts[1],
                cell_texts[2],
                cell_texts[3],
            )
        else:
            continue
        # Extract title from Wikipedia's "Title" ‡ (#peak) annotation format
        m = re.search(r'[""](.+?)[""]', raw_title)
        title_text = (
            m.group(1).strip()
            if m
            else re.sub(r"[‡♦††‡].*$|\(#\d+\)", "", raw_title).strip()
        )
        if not title_text or not artist_text:
            continue
        try:
            peak = int(re.sub(r"\D", "", peak_text))
            if not (1 <= peak <= 10):
                continue
        except (ValueError, TypeError):
            continue
        key = normalize_for_dedup(artist_text) + "|||" + normalize_for_dedup(title_text)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            {"rank": peak, "title": title_text, "artist_raw": artist_text, "year": year}
        )
    if records:
        print(f"    INFO [spotify_uk_wiki {year}]: {len(records)} UK top-10 songs")
    return records


def _parse_spotify_global(
    year: int, spotify_token: str | None, client: httpx.Client
) -> list[dict]:
    if spotify_token:
        records = _parse_spotify_source("spotify_global", year, "global", spotify_token)
        if records:
            return records
    return _parse_spotify_global_wiki(year, client)


def _parse_spotify_uk(
    year: int, spotify_token: str | None, client: httpx.Client
) -> list[dict]:
    if spotify_token:
        records = _parse_spotify_source("spotify_uk", year, "uk", spotify_token)
        if records:
            return records
    return _parse_uk_top10_wiki(year, client)


# ---------------------------------------------------------------------------
# Bonus sources (skip gracefully on failure)
# ---------------------------------------------------------------------------


def _parse_spotify_us(year: int, spotify_token: str | None) -> list[dict]:
    records = _parse_spotify_source("spotify_us", year, "us", spotify_token)
    if not records:
        print(f"    INFO [spotify_us {year}]: Unavailable — skipping")
    return records  # no wiki fallback for US; overlap with Billboard makes it low-value


def _parse_apple_music(year: int) -> list[dict]:
    print(f"    INFO [apple_music {year}]: No reliable public source found — skipping")
    return []


def _parse_youtube_music(year: int) -> list[dict]:
    print(f"    INFO [youtube_music {year}]: Coverage is patchy — skipping")
    return []


# ---------------------------------------------------------------------------
# Per-year data collection
# ---------------------------------------------------------------------------


def _collect_year(
    year: int, client: httpx.Client, spotify_token: str | None
) -> tuple[list[tuple[str, list[dict]]], list[str]]:
    """Fetch all source data for one year.

    Returns (list of (source_key, records), list of bonus_sources_that_succeeded).
    """
    results: list[tuple[str, list[dict]]] = []
    bonus_succeeded: list[str] = []

    # --- mandatory: Billboard Hot 100 ---
    hot100 = _parse_billboard_hot100(year, client)
    if hot100:
        results.append(("billboard_hot_100", hot100))
    elif year < PARTIAL_YEAR:
        print(f"    WARNING [billboard_hot_100 {year}]: 0 records (mandatory)")
    time.sleep(0.3)

    # --- mandatory: Billboard genre charts ---
    for genre_key in _GENRE_WIKI_SLUGS:
        records = _parse_billboard_genre(genre_key, year, client)
        if records:
            results.append((genre_key, records))
        time.sleep(0.3)

    # --- mandatory: Spotify Global ---
    sp_global = _parse_spotify_global(year, spotify_token, client)
    if sp_global:
        results.append(("spotify_global", sp_global))
    elif year < PARTIAL_YEAR:
        print(f"    WARNING [spotify_global {year}]: 0 records (mandatory)")
    time.sleep(0.2)

    # --- mandatory: Spotify UK ---
    sp_uk = _parse_spotify_uk(year, spotify_token, client)
    if sp_uk:
        results.append(("spotify_uk", sp_uk))
    elif year < PARTIAL_YEAR:
        print(f"    WARNING [spotify_uk {year}]: 0 records (mandatory)")
    time.sleep(0.2)

    # --- bonus: Spotify US ---
    sp_us = _parse_spotify_us(year, spotify_token)
    if sp_us:
        results.append(("spotify_us", sp_us))
        bonus_succeeded.append("spotify_us")

    # --- bonus: Apple Music ---
    apple = _parse_apple_music(year)
    if apple:
        results.append(("apple_music", apple))
        bonus_succeeded.append("apple_music")

    # --- bonus: YouTube Music ---
    yt = _parse_youtube_music(year)
    if yt:
        results.append(("youtube_music", yt))
        bonus_succeeded.append("youtube_music")

    return results, bonus_succeeded


# ---------------------------------------------------------------------------
# Scoring and union logic
# ---------------------------------------------------------------------------


def _compute_year_candidates(
    year: int, source_results: list[tuple[str, list[dict]]]
) -> pd.DataFrame:
    """Union all source results for a year into a scored candidate DataFrame.

    Each unique (normalized_artist, normalized_title) pair becomes one row.
    combined_score = sum of weighted normalized scores across all sources.
    """
    candidates: dict[str, dict] = {}

    for source_key, records in source_results:
        n = len(records)
        if n == 0:
            continue
        weight = SOURCE_WEIGHTS.get(source_key, 0.4)
        for rec in records:
            rank = rec["rank"]
            artist, featured = parse_featured_artists(str(rec["artist_raw"]))
            title = str(rec["title"]).strip()

            a_key = normalize_for_dedup(artist)
            t_key = normalize_for_dedup(title)
            key = f"{a_key}|||{t_key}"

            # normalized score: rank 1 → 1.0, rank N → 1/N
            norm_score = (n + 1 - rank) / n
            weighted = norm_score * weight

            if key not in candidates:
                candidates[key] = {
                    "_key": key,
                    "_artist_key": a_key,
                    "_title_key": t_key,
                    "artist": artist,
                    "featured_artists": featured,
                    "title": title,
                    "year": year,
                    "combined_score": 0.0,
                    "sources": [],
                }
            else:
                # Prefer non-null featured_artists if we get it from a later source
                if featured and not candidates[key]["featured_artists"]:
                    candidates[key]["featured_artists"] = featured

            candidates[key]["combined_score"] += weighted
            if source_key not in candidates[key]["sources"]:
                candidates[key]["sources"].append(source_key)

    if not candidates:
        return pd.DataFrame()

    df = pd.DataFrame(candidates.values())
    df["source"] = df["sources"].apply(lambda s: ", ".join(s))
    df = df.drop(columns=["sources"])
    df = df.sort_values("combined_score", ascending=False).head(TOP_N_PER_YEAR)
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Alternate-version filtering
# ---------------------------------------------------------------------------


def _drop_alternate_versions(df: pd.DataFrame) -> pd.DataFrame:
    """Drop live/remix/demo versions when the studio original is present."""
    is_alt = df["title"].apply(lambda t: bool(_VERSION_RE.search(t)))
    base_keys: set[str] = set(df.loc[~is_alt, "_key"])

    to_drop: list = []
    for idx, row in df[is_alt].iterrows():
        stripped = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", row["title"]).strip()
        candidate = row["_artist_key"] + "|||" + normalize_for_dedup(stripped)
        if candidate in base_keys:
            to_drop.append(idx)

    return df.drop(index=to_drop)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("=== Tier 3: Recent Popular (2015-2026) ===")
    print("Sources: Billboard (Wikipedia) + Spotify (API) per year...\n")

    print("Authenticating with Spotify API...")
    spotify_token = _spotify_token()
    if spotify_token:
        print("  Spotify token acquired.")
    else:
        print("  Spotify token unavailable — Spotify sources will be skipped.")

    all_year_dfs: list[pd.DataFrame] = []
    year_source_counts: dict[int, dict[str, int]] = {}
    year_prededup_counts: dict[int, int] = {}
    year_bonus_success: dict[int, list[str]] = {}

    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        for year in YEARS:
            print(f"\n--- {year} ---")
            source_results, bonus_succeeded = _collect_year(year, client, spotify_token)
            year_bonus_success[year] = bonus_succeeded

            year_source_counts[year] = {sk: len(recs) for sk, recs in source_results}
            for sk, count in year_source_counts[year].items():
                print(f"  {sk}: {count}")

            if not source_results:
                print(f"  SKIP: no data for {year}")
                year_prededup_counts[year] = 0
                continue

            year_df = _compute_year_candidates(year, source_results)
            if year_df.empty:
                print(f"  SKIP: empty after scoring for {year}")
                year_prededup_counts[year] = 0
                continue

            year_df = _drop_alternate_versions(year_df)
            year_prededup_counts[year] = len(year_df)
            print(
                f"  → {len(year_df)} songs after union + scoring + alt-version filter"
            )

            all_year_dfs.append(year_df)
            time.sleep(0.5)

    if not all_year_dfs:
        print("ERROR: No data collected for any year.")
        sys.exit(1)

    combined = pd.concat(all_year_dfs, ignore_index=True)

    # Cross-year dedup: keep the entry with the highest combined_score per song
    combined = combined.sort_values("combined_score", ascending=False).drop_duplicates(
        subset="_key", keep="first"
    )

    # Dedup against Tier 1
    print(f"\nLoading {TIER1_PATH} ...")
    t1 = pd.read_csv(TIER1_PATH)
    t1_keys: set[str] = {
        normalize_for_dedup(str(r["artist"]))
        + "|||"
        + normalize_for_dedup(str(r["title"]))
        for _, r in t1.iterrows()
    }
    in_t1 = combined["_key"].isin(t1_keys)
    n_removed_t1 = int(in_t1.sum())
    combined = combined[~in_t1].copy()
    print(f"  Removed {n_removed_t1} rows matching Tier 1")

    # Dedup against Tier 2
    print(f"Loading {TIER2_PATH} ...")
    t2 = pd.read_csv(TIER2_PATH)
    t2_keys: set[str] = {
        normalize_for_dedup(str(r["artist"]))
        + "|||"
        + normalize_for_dedup(str(r["title"]))
        for _, r in t2.iterrows()
    }
    in_t2 = combined["_key"].isin(t2_keys)
    n_removed_t2 = int(in_t2.sum())
    combined = combined[~in_t2].copy()
    print(f"  Removed {n_removed_t2} rows matching Tier 2")

    # Finalise output schema
    combined = combined.rename(columns={"combined_score": "source_rank"})
    combined["tier"] = "recent"
    final = combined[
        ["artist", "featured_artists", "title", "year", "source", "source_rank", "tier"]
    ].reset_index(drop=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    final.to_csv(OUTPUT_PATH, index=False)

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------

    print("\n=== Per-Year Source Counts ===")
    for year in YEARS:
        counts = year_source_counts.get(year, {})
        bonus = year_bonus_success.get(year, [])
        pre = year_prededup_counts.get(year, 0)
        post = int((final["year"] == year).sum())
        parts = ", ".join(f"{sk}:{n}" for sk, n in counts.items())
        bonus_str = f"  [bonus: {', '.join(bonus)}]" if bonus else ""
        print(f"  {year}: {parts} → {pre} before dedup → {post} final{bonus_str}")

    feat_count = int(final["featured_artists"].notna().sum())
    multi_source = int(final["source"].str.contains(",").sum())

    # Rows that include at least one non-Billboard source
    billboard_sources = {
        "billboard_hot_100",
        "billboard_rap",
        "billboard_country",
        "billboard_latin",
        "billboard_rnb",
        "billboard_dance",
        "billboard_rock",
    }
    has_non_billboard = final["source"].apply(
        lambda s: any(src.strip() not in billboard_sources for src in s.split(","))
    )
    n_non_billboard = int(has_non_billboard.sum())

    print("\n=== Summary ===")
    print(f"  Rows removed (Tier 1 overlap):     {n_removed_t1}")
    print(f"  Rows removed (Tier 2 overlap):     {n_removed_t2}")
    print(f"  Total final row count:             {len(final)}")
    print(f"  Rows with featured artists:        {feat_count}")
    print(f"  Rows with multiple sources:        {multi_source}")
    print(f"  Rows with ≥1 non-Billboard source: {n_non_billboard}")
    print(f"  Output: {OUTPUT_PATH}")

    # ---------------------------------------------------------------------------
    # Success criteria
    # ---------------------------------------------------------------------------

    print("\n=== Success Criteria ===")
    all_ok = True

    count_ok = 4500 <= len(final) <= 5000
    print(
        f"  {'OK' if count_ok else 'FAIL'} Row count {len(final)} (expected 4,500-5,000)"
    )
    all_ok = all_ok and count_ok

    no_dupes = not final.duplicated(subset=["artist", "title"]).any()
    print(f"  {'OK' if no_dupes else 'FAIL'} No exact (artist, title) duplicates")
    all_ok = all_ok and no_dupes

    per_year_ok = True
    for year in range(2015, 2026):
        n = int((final["year"] == year).sum())
        if n < 300:
            print(f"  FAIL Year {year}: only {n} rows (need ≥300)")
            per_year_ok = False
    print(f"  {'OK' if per_year_ok else 'FAIL'} Each year 2015-2025 has ≥300 rows")
    all_ok = all_ok and per_year_ok

    final_keys = (
        final["artist"].apply(normalize_for_dedup)
        + "|||"
        + final["title"].apply(normalize_for_dedup)
    )
    t1_clean = not final_keys.isin(t1_keys).any()
    t2_clean = not final_keys.isin(t2_keys).any()
    print(f"  {'OK' if t1_clean else 'FAIL'} No Tier 1 songs in Tier 3")
    print(f"  {'OK' if t2_clean else 'FAIL'} No Tier 2 songs in Tier 3")
    all_ok = all_ok and t1_clean and t2_clean

    multi_ok = multi_source >= 500
    print(
        f"  {'OK' if multi_ok else 'FAIL'} Multi-source rows: {multi_source} (need ≥500)"
    )
    all_ok = all_ok and multi_ok

    non_bill_ok = n_non_billboard >= 200
    print(
        f"  {'OK' if non_bill_ok else 'FAIL'} Non-Billboard-only rows: {n_non_billboard} (need ≥200)"
    )
    all_ok = all_ok and non_bill_ok

    # Era sanity checks
    a_keys = final["artist"].apply(normalize_for_dedup)
    t_keys = final["title"].apply(normalize_for_dedup)
    sanity_checks = [
        ("Levitating", "Dua Lipa"),
        ("Heat Waves", "Glass Animals"),
        ("Anti-Hero", "Taylor Swift"),
        ("Flowers", "Miley Cyrus"),
        ("Espresso", "Sabrina Carpenter"),
    ]
    found = 0
    for title, artist in sanity_checks:
        ak = normalize_for_dedup(artist)
        tk = normalize_for_dedup(title)
        song_key = f"{ak}|||{tk}"
        in_t3 = ((a_keys == ak) & (t_keys == tk)).any()
        in_earlier = song_key in t1_keys or song_key in t2_keys
        if in_t3:
            print(f"  OK  '{title}' by {artist} — in Tier 3")
            found += 1
        elif in_earlier:
            print(f"  OK  '{title}' by {artist} — in earlier tier (correctly dedup'd)")
            found += 1
        else:
            print(f"  FAIL '{title}' by {artist} — not found in any tier!")

    sanity_ok = found >= 3
    print(
        f"  {'OK' if sanity_ok else 'FAIL'} Era sanity checks: {found}/5 found (need ≥3)"
    )
    all_ok = all_ok and sanity_ok

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
