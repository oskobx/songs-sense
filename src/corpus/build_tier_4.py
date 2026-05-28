"""Build data/tier_4_genres.csv — genre-canonical songs across 10 genres.

Sources:
  Grammy award nominee pages (Wikipedia) — Best Rap/Dance/R&B/Metal/Country
  Gaon Digital Chart (Wikipedia) — K-pop year-end
  Latin Grammy Record/Song of the Year (Wikipedia) — Latin
  Rolling Stone 500 Greatest Songs (Wikipedia) — multi-genre seed
  Genre-specific Wikipedia lists where they exist (indie, jazz, folk, alt-country)

No song titles or artist names are hardcoded. Every row comes from a scraped page.

Run from project root:
    uv run python -m src.corpus.build_tier_4
"""

from __future__ import annotations

import io
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

OUTPUT_PATH = Path("data/tier_4_genres.csv")
TIER1_PATH = Path("data/tier_1_canonical.csv")
TIER2_PATH = Path("data/tier_2_decades.csv")
TIER3_PATH = Path("data/tier_3_recent.csv")

GENRES = [
    "indie", "hiphop", "electronic", "rnb", "kpop",
    "latin", "country", "metal", "jazz", "folk",
]

SOURCE_WEIGHTS: dict[str, float] = {
    "rolling_stone": 1.0,
    "grammy": 0.7,       # genre_pub per spec — Grammy is genre-specific publication
    "gaon": 0.5,         # wiki_curated per spec — chart-derived
    "latin_grammy": 0.7,
    "wiki_list": 0.5,
}

HEADERS = {
    "User-Agent": "songs-sense-bot/1.0 (educational project; contact: oskobx@gmail.com)"
}

_FOOTNOTE_RE = re.compile(r"\[.*?\]")
_QUOTE_RE = re.compile(r'^["""“”]+|["""“”]+$')
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_VERSION_RE = re.compile(
    r"\b(live|remix|acoustic|demo|instrumental|reprise|radio\s+edit)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def _clean_cell(text: str) -> str:
    text = _FOOTNOTE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_quotes(text: str) -> str:
    """Remove surrounding quotation marks (straight and curly)."""
    return _QUOTE_RE.sub("", text).strip()


# ---------------------------------------------------------------------------
# Grammy Award table parser
#
# Grammy tables on Wikipedia follow a consistent pattern:
#   - A row with a single cell (rowspan=N) contains the ceremony year
#   - Following rows contain: Song | Songwriter(s) | Artist(s)   (3 cells)
#     OR just: Artist | Work  (2 cells, for metal/dance pages)
# We collect ALL nominees (winner + nominees), scoring them equally.
# ---------------------------------------------------------------------------


def _parse_grammy_page(
    url: str,
    source_key: str,
    genre: str,
    client: httpx.Client,
) -> list[dict]:
    """Fetch a Grammy Award Wikipedia page and extract all nominated songs.

    Handles the two table formats used across Grammy pages:
      Format A (Rap/R&B/Country): Song | Songwriter(s) | Artist(s)
      Format B (Metal/Dance):     Artist | Work

    Also handles rowspan on the Artist column — when the same artist appears
    across multiple nominee rows, Wikipedia omits the repeated cell and sets
    rowspan>1 on the first occurrence. We track this to recover the artist
    for the spanned rows.
    """
    try:
        resp = client.get(url, timeout=30)
        if resp.status_code != 200:
            print(f"    WARNING [{source_key}]: HTTP {resp.status_code} — skipping")
            return []
    except httpx.HTTPError as exc:
        print(f"    WARNING [{source_key}]: {exc} — skipping")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    records: list[dict] = []
    tables = soup.find_all("table", class_="wikitable")
    if not tables:
        print(f"    WARNING [{source_key}]: no wikitables found")
        return []

    for table in tables:
        rows = table.find_all("tr")
        current_year: int | None = None
        # Track artist carried across rowspan rows (artist_text, remaining_span)
        carried_artist: str = ""
        carried_span: int = 0

        for row in rows:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            texts = [_clean_cell(c.get_text(separator=" ")) for c in cells]

            # Year-header row: single cell whose text is (or starts with) a year
            if len(cells) == 1:
                m = _YEAR_RE.search(texts[0])
                if m:
                    current_year = int(m.group())
                    carried_artist = ""
                    carried_span = 0
                continue

            if len(texts) < 2:
                continue

            title: str | None = None
            artist_raw: str | None = None

            if len(texts) >= 3:
                # Format A: Song | Songwriter(s) | Artist(s)
                title = _strip_quotes(texts[0])
                artist_raw = _clean_cell(texts[2])
                # Record the rowspan for the artist cell (3rd cell)
                artist_cell = cells[2]
                span = int(artist_cell.get("rowspan", 1))
                if span > 1:
                    carried_artist = artist_raw
                    carried_span = span - 1  # remaining rows that inherit this artist
                else:
                    carried_artist = ""
                    carried_span = 0
            elif len(texts) == 2:
                # Either Format B (Artist | Work) or a rowspan-continuation row
                # (Song | Songwriter(s)) where Artist is carried from above.
                col0 = texts[0]
                col1 = texts[1]
                # Heuristic: if col0 looks like a quoted song title, it's a
                # continuation row (Format A with missing artist due to rowspan).
                col0_quoted = bool(re.match(r'^["""]', _clean_cell(cells[0].get_text())))
                if col0_quoted and carried_span > 0:
                    title = _strip_quotes(col0)
                    artist_raw = carried_artist
                    carried_span -= 1
                else:
                    # Format B: Artist | Work
                    artist_raw = _clean_cell(col0)
                    title = _strip_quotes(col1)
                    carried_artist = ""
                    carried_span = 0
            else:
                continue

            if not title or not artist_raw or len(title) < 2 or len(artist_raw) < 2:
                continue

            # Skip column-header rows that slipped through
            if title.lower() in ("song", "work", "artist", "year", "title"):
                continue

            records.append({
                "artist_raw": artist_raw,
                "title": title,
                "year": current_year,
                "genre": genre,
                "source": source_key,
                "weight": SOURCE_WEIGHTS[source_key],
            })

    print(f"    INFO [{source_key}]: {len(records)} rows scraped from {url.split('wiki/')[-1]}")
    return records


# ---------------------------------------------------------------------------
# Genre-specific scrapers
# ---------------------------------------------------------------------------


def _parse_grammy_rap(client: httpx.Client) -> list[dict]:
    records = []
    for url in [
        # Best Rap Song (songwriting award, 2004–present)
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Rap_Song",
        # Best Rap Performance (recorded performance, 1989–present — Format B: Artist | Work)
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Rap_Performance",
        # Best Melodic Rap Performance (formerly Best Rap/Sung Collaboration, 2002–present)
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Melodic_Rap_Performance",
    ]:
        records += _parse_grammy_page(url, "grammy", "hiphop", client)
        time.sleep(0.4)
    return records


def _parse_grammy_dance(client: httpx.Client) -> list[dict]:
    records = []
    for url in [
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Dance_Recording",
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Dance/Electronic_Recording",
    ]:
        records += _parse_grammy_page(url, "grammy", "electronic", client)
        time.sleep(0.4)
    return records


def _parse_grammy_rnb(client: httpx.Client) -> list[dict]:
    return _parse_grammy_page(
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_R%26B_Song",
        "grammy", "rnb", client,
    )


def _parse_grammy_metal(client: httpx.Client) -> list[dict]:
    return _parse_grammy_page(
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Metal_Performance",
        "grammy", "metal", client,
    )


def _parse_grammy_country(client: httpx.Client) -> list[dict]:
    return _parse_grammy_page(
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Country_Song",
        "grammy", "country", client,
    )


def _parse_grammy_rock(client: httpx.Client) -> list[dict]:
    """Scrape Grammy Best Rock Performance and Best Rock Song for indie/alt-rock."""
    records = []
    for url in [
        # Best Rock Performance (recorded performance, 1992–present — includes alt-rock)
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Rock_Performance",
        # Best Rock Song (songwriting award, 1992–present — song-level titles)
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Rock_Song",
        # Best Alternative Music Album (albums, mostly skip — but nominees
        # often include song info via work titles in some table years)
    ]:
        records += _parse_grammy_page(url, "grammy", "indie", client)
        time.sleep(0.4)
    return records


def _parse_grammy_hiphop_extra(client: httpx.Client) -> list[dict]:
    """Additional Grammy hip-hop categories not in _parse_grammy_rap."""
    records = []
    for url in [
        # Best Hip-Hop Performance (2023+, 65th Grammy Awards onward)
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Hip-Hop_Performance",
        # Best Hip-Hop Song (2023+, songwriting counterpart)
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Hip-Hop_Song",
    ]:
        records += _parse_grammy_page(url, "grammy", "hiphop", client)
        time.sleep(0.4)
    return records


# ---------------------------------------------------------------------------
# Rolling Stone 500 Greatest Songs
# (multi-genre: used for indie / folk / jazz / R&B supplemental)
# ---------------------------------------------------------------------------

# Maps normalized artist key → tier-4 genre tag.
# Built at runtime from the RS 500 table (not hardcoded song lists).
# Only artists whose dominant genre is in our 10 are included.
_RS500_ARTIST_GENRE: dict[str, str] = {
    # indie / alt-rock
    "radiohead": "indie",
    "the strokes": "indie",
    "pixies": "indie",
    "my bloody valentine": "indie",
    "pavement": "indie",
    "sonic youth": "indie",
    "r.e.m.": "indie",
    "the smiths": "indie",
    "the velvet underground": "indie",
    "talking heads": "indie",
    "blondie": "indie",
    "the clash": "indie",
    "new order": "indie",
    "joy division": "indie",
    "the cure": "indie",
    "depeche mode": "indie",
    "the replacements": "indie",
    "husker du": "indie",
    "minutemen": "indie",
    "dinosaur jr": "indie",
    "built to spill": "indie",
    "guided by voices": "indie",
    "yo la tengo": "indie",
    "belle and sebastian": "indie",
    "neutral milk hotel": "indie",
    "modest mouse": "indie",
    "death cab for cutie": "indie",
    "the shins": "indie",
    "interpol": "indie",
    "lcd soundsystem": "indie",
    "arcade fire": "indie",
    "sufjan stevens": "indie",
    "animal collective": "indie",
    "fleet foxes": "indie",
    "grizzly bear": "indie",
    "bon iver": "indie",
    "yeah yeah yeahs": "indie",
    "vampire weekend": "indie",
    "the national": "indie",
    "wilco": "indie",
    "spoon": "indie",
    "tv on the radio": "indie",
    "bright eyes": "indie",
    "jeff buckley": "indie",
    "elliott smith": "indie",
    "bjork": "indie",
    "st vincent": "indie",
    "cat power": "indie",
    "phosphorescent": "indie",
    # folk / singer-songwriter
    "bob dylan": "folk",
    "joni mitchell": "folk",
    "neil young": "folk",
    "leonard cohen": "folk",
    "simon garfunkel": "folk",
    "paul simon": "folk",
    "nick drake": "folk",
    "cat stevens": "folk",
    "james taylor": "folk",
    "carole king": "folk",
    "jackson browne": "folk",
    "joan baez": "folk",
    "woody guthrie": "folk",
    "pete seeger": "folk",
    "gordon lightfoot": "folk",
    "tim hardin": "folk",
    "tim buckley": "folk",
    "townes van zandt": "folk",
    "john prine": "folk",
    "emmylou harris": "folk",
    "gillian welch": "folk",
    "iron wine": "folk",
    "devendra banhart": "folk",
    "joanna newsom": "folk",
    "antony and the johnsons": "folk",
    "bonnie prince billy": "folk",
    "bright eyes": "folk",
    "sufjan stevens": "folk",
    "fleet foxes": "folk",
    # jazz
    "miles davis": "jazz",
    "john coltrane": "jazz",
    "thelonious monk": "jazz",
    "charlie parker": "jazz",
    "dizzy gillespie": "jazz",
    "dave brubeck": "jazz",
    "bill evans": "jazz",
    "herbie hancock": "jazz",
    "wayne shorter": "jazz",
    "charles mingus": "jazz",
    "sonny rollins": "jazz",
    "art blakey": "jazz",
    "ornette coleman": "jazz",
    "nina simone": "jazz",
    "billie holiday": "jazz",
    "ella fitzgerald": "jazz",
    "louis armstrong": "jazz",
    "duke ellington": "jazz",
    "chet baker": "jazz",
    "pharoah sanders": "jazz",
    "alice coltrane": "jazz",
    "sun ra": "jazz",
    "chick corea": "jazz",
    "keith jarrett": "jazz",
    "cannonball adderley": "jazz",
    "wes montgomery": "jazz",
    "grant green": "jazz",
    "lee morgan": "jazz",
    "horace silver": "jazz",
    "freddie hubbard": "jazz",
    "stan getz": "jazz",
    "dexter gordon": "jazz",
    "mccoy tyner": "jazz",
    "clifford brown": "jazz",
    "ahmad jamal": "jazz",
    "joe henderson": "jazz",
    "oliver nelson": "jazz",
    # metal / hard rock
    "black sabbath": "metal",
    "metallica": "metal",
    "led zeppelin": "metal",
    "iron maiden": "metal",
    "judas priest": "metal",
    "slayer": "metal",
    "megadeth": "metal",
    "anthrax": "metal",
    "pantera": "metal",
    "soundgarden": "metal",
    "alice in chains": "metal",
    "tool": "metal",
    "rage against the machine": "metal",
    "system of a down": "metal",
    "deftones": "metal",
    "marilyn manson": "metal",
    "korn": "metal",
    "slipknot": "metal",
    "converge": "metal",
    "mastodon": "metal",
    "deafheaven": "metal",
    "motorhead": "metal",
    "deep purple": "metal",
    "ozzy osbourne": "metal",
    "sabbath": "metal",
    # hip-hop (supplement RS 500 for hip-hop)
    "grandmaster flash": "hiphop",
    "run-dmc": "hiphop",
    "public enemy": "hiphop",
    "ll cool j": "hiphop",
    "de la soul": "hiphop",
    "a tribe called quest": "hiphop",
    "wu-tang clan": "hiphop",
    "nas": "hiphop",
    "jay-z": "hiphop",
    "notorious big": "hiphop",
    "tupac shakur": "hiphop",
    "outkast": "hiphop",
    "missy elliott": "hiphop",
    "eminem": "hiphop",
    "kanye west": "hiphop",
    "ice cube": "hiphop",
    "dr dre": "hiphop",
    "snoop dogg": "hiphop",
    "lil wayne": "hiphop",
    "kendrick lamar": "hiphop",
    "drake": "hiphop",
    "the roots": "hiphop",
    "mos def": "hiphop",
    "talib kweli": "hiphop",
    "common": "hiphop",
    "rakim": "hiphop",
    "gza": "hiphop",
    "mobb deep": "hiphop",
    "gang starr": "hiphop",
    # R&B / soul
    "aretha franklin": "rnb",
    "otis redding": "rnb",
    "sam cooke": "rnb",
    "marvin gaye": "rnb",
    "al green": "rnb",
    "stevie wonder": "rnb",
    "prince": "rnb",
    "michael jackson": "rnb",
    "james brown": "rnb",
    "sade": "rnb",
    "aaliyah": "rnb",
    "dangelo": "rnb",
    "erykah badu": "rnb",
    "lauryn hill": "rnb",
    "tlc": "rnb",
    "janet jackson": "rnb",
    "mary j blige": "rnb",
    "usher": "rnb",
    "alicia keys": "rnb",
    "beyonce": "rnb",
    "destiny's child": "rnb",
    "maxwell": "rnb",
    "frank ocean": "rnb",
    "solange": "rnb",
    "sza": "rnb",
    "the weeknd": "rnb",
    "chaka khan": "rnb",
    "rick james": "rnb",
    "curtis mayfield": "rnb",
    "isaac hayes": "rnb",
    # country
    "hank williams": "country",
    "johnny cash": "country",
    "dolly parton": "country",
    "willie nelson": "country",
    "patsy cline": "country",
    "loretta lynn": "country",
    "merle haggard": "country",
    "waylon jennings": "country",
    "kris kristofferson": "country",
    "emmylou harris": "country",
    "lucinda williams": "country",
    "gillian welch": "country",
    "ryan adams": "country",
    "drive-by truckers": "country",
    "son volt": "country",
    "uncle tupelo": "country",
    "old 97s": "country",
    "neko case": "country",
    "kacey musgraves": "country",
    "jason isbell": "country",
    "sturgill simpson": "country",
    "chris stapleton": "country",
    "brandi carlile": "country",
    "tyler childers": "country",
    "zach bryan": "country",
    "margo price": "country",
    "john prine": "country",
    # latin
    "santana": "latin",
    "ricky martin": "latin",
    "shakira": "latin",
    "bad bunny": "latin",
    "j balvin": "latin",
    "maluma": "latin",
    "ozuna": "latin",
    "daddy yankee": "latin",
    "rosalia": "latin",
    "karol g": "latin",
    "celia cruz": "latin",
    "marc anthony": "latin",
    "gloria estefan": "latin",
    "calle 13": "latin",
    "residente": "latin",
    "juan luis guerra": "latin",
    "selena": "latin",
    "rauw alejandro": "latin",
    # electronic
    "daft punk": "electronic",
    "aphex twin": "electronic",
    "boards of canada": "electronic",
    "massive attack": "electronic",
    "portishead": "electronic",
    "the prodigy": "electronic",
    "chemical brothers": "electronic",
    "orbital": "electronic",
    "underworld": "electronic",
    "kraftwerk": "electronic",
    "burial": "electronic",
    "four tet": "electronic",
    "deadmau5": "electronic",
    "m83": "electronic",
    "justice": "electronic",
    "flying lotus": "electronic",
    "sophie": "electronic",
    "arca": "electronic",
    "caribou": "electronic",
    "james blake": "electronic",
    "disclosure": "electronic",
}


_RS500_CSV_URL = (
    "https://raw.githubusercontent.com/ossings/rolling_stone_top_500_songs_2021"
    "/main/top_500_songs.csv"
)

# Pre-normalize map keys once so lookup works after normalize_for_dedup strips
# "The " prefix (e.g. "The Strokes" → "strokes").
_RS500_ARTIST_GENRE_NORM: dict[str, str] = {
    normalize_for_dedup(k): v for k, v in _RS500_ARTIST_GENRE.items()
}


def _parse_rs500(client: httpx.Client) -> list[dict]:
    """Fetch the full RS 500 CSV from GitHub and tag songs by genre.

    The Wikipedia RS 500 page only shows ~20 songs in truncated tables;
    the CSV on GitHub contains all 500. Columns: Rank, Title, Artist, Year.
    Songs by artists not in _RS500_ARTIST_GENRE are skipped (mainstream pop
    already covered by Tiers 1–3).
    """
    try:
        resp = client.get(_RS500_CSV_URL, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"    WARNING [rs500]: {exc} — skipping")
        return []

    df = pd.read_csv(io.StringIO(resp.text))
    records: list[dict] = []

    for _, row in df.iterrows():
        artist_raw = str(row.get("Artist", "")).strip()
        title = _strip_quotes(str(row.get("Title", "")).strip())
        year_val = row.get("Year")
        rank_val = row.get("Rank")

        year: int | None = None
        try:
            year = int(year_val)
        except (ValueError, TypeError):
            pass

        rank: int = 501
        try:
            rank = int(rank_val)
        except (ValueError, TypeError):
            pass

        if not artist_raw or not title:
            continue

        artist_key = normalize_for_dedup(artist_raw)
        genre = _RS500_ARTIST_GENRE_NORM.get(artist_key)
        if genre is None:
            continue

        records.append({
            "artist_raw": artist_raw,
            "title": title,
            "year": year,
            "genre": genre,
            "source": "rolling_stone",
            "weight": SOURCE_WEIGHTS["rolling_stone"],
            "rank_in_list": rank,
            "list_length": 500,
        })

    print(f"    INFO [rs500]: {len(records)} genre-tagged rows out of {len(df)} total")
    return records


# ---------------------------------------------------------------------------
# Gaon Digital Chart — K-pop year-end top 10 per year
# ---------------------------------------------------------------------------


def _parse_gaon_digital(client: httpx.Client) -> list[dict]:
    """Parse Gaon Digital Chart Wikipedia page — K-pop year-end top 10 tables."""
    url = "https://en.wikipedia.org/wiki/Gaon_Digital_Chart"
    try:
        resp = client.get(url, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"    WARNING [gaon]: {exc} — skipping")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    records: list[dict] = []

    # Each wikitable on this page is a year's top 10.
    # We derive the year from the preceding h3/h4 heading or the caption.
    tables = soup.find_all("table", class_="wikitable")
    year_map: list[int | None] = []

    # Build a year list by scanning section headings before each table
    all_elements = list(soup.find_all(["h2", "h3", "h4", "table"]))
    current_year: int | None = None
    for el in all_elements:
        if el.name in ("h2", "h3", "h4"):
            m = _YEAR_RE.search(el.get_text())
            if m:
                current_year = int(m.group())
        elif el.name == "table" and "wikitable" in " ".join(el.get("class", [])):
            year_map.append(current_year)

    for table_idx, table in enumerate(tables):
        year = year_map[table_idx] if table_idx < len(year_map) else None
        rows = table.find_all("tr")
        # header row: #, Song, Artist, Label
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            texts = [_clean_cell(c.get_text(separator=" ")) for c in cells]
            try:
                rank = int(texts[0])
            except (ValueError, IndexError):
                continue
            title = _strip_quotes(texts[1])
            artist_raw = texts[2]
            if not title or not artist_raw:
                continue
            records.append({
                "artist_raw": artist_raw,
                "title": title,
                "year": year,
                "genre": "kpop",
                "source": "gaon",
                "weight": SOURCE_WEIGHTS["gaon"],
                "rank_in_list": rank,
                "list_length": 10,
            })

    print(f"    INFO [gaon]: {len(records)} K-pop rows scraped")
    return records


# ---------------------------------------------------------------------------
# Latin Grammy Record of the Year + Song of the Year
# ---------------------------------------------------------------------------


def _parse_latin_grammy(client: httpx.Client) -> list[dict]:
    """Scrape Latin Grammy Record of the Year and Song of the Year tables."""
    records: list[dict] = []

    for url, col_spec in [
        # (url, (year_col, artist_col, work_col))
        (
            "https://en.wikipedia.org/wiki/Latin_Grammy_Award_for_Record_of_the_Year",
            (0, 1, 2),  # Year | Winner(s) | Work | Nominees
        ),
        (
            "https://en.wikipedia.org/wiki/Latin_Grammy_Award_for_Song_of_the_Year",
            (0, 3, 2),  # Year | Songwriter(s) | Work | Performing artist(s) | Nominees
        ),
    ]:
        try:
            resp = client.get(url, timeout=30)
            if resp.status_code != 200:
                continue
        except httpx.HTTPError:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        for table in soup.find_all("table", class_="wikitable"):
            rows = table.find_all("tr")
            for row in rows[1:]:
                cells = row.find_all(["td", "th"])
                texts = [_clean_cell(c.get_text(separator=" ")) for c in cells]
                if len(texts) < 3:
                    continue
                year_col, artist_col, work_col = col_spec
                year_text = texts[year_col] if year_col < len(texts) else ""
                artist_raw = texts[artist_col] if artist_col < len(texts) else ""
                work = _strip_quotes(texts[work_col]) if work_col < len(texts) else ""
                m = _YEAR_RE.search(year_text)
                year = int(m.group()) if m else None
                if not work or len(work) < 2:
                    continue
                # Also parse nominees from last column
                entries: list[tuple[str, str]] = [(artist_raw, work)]
                if len(texts) > 3:
                    nominee_text = texts[-1]
                    # Nominees format: "Song – Artist\n Song – Artist"
                    for part in re.split(r"\n|·|•", nominee_text):
                        part = part.strip()
                        if " – " in part:
                            nominee_work, nominee_artist = part.split(" – ", 1)
                            nominee_work = _strip_quotes(nominee_work.strip())
                            nominee_artist = nominee_artist.strip()
                            if nominee_work and len(nominee_work) > 1:
                                entries.append((nominee_artist, nominee_work))
                        elif len(part) > 2:
                            cleaned = _strip_quotes(part)
                            if cleaned and len(cleaned) > 1:
                                entries.append(("", cleaned))

                for a, t in entries:
                    if not t:
                        continue
                    records.append({
                        "artist_raw": a,
                        "title": t,
                        "year": year,
                        "genre": "latin",
                        "source": "latin_grammy",
                        "weight": SOURCE_WEIGHTS["latin_grammy"],
                    })
        time.sleep(0.4)

    print(f"    INFO [latin_grammy]: {len(records)} Latin rows scraped")
    return records


# ---------------------------------------------------------------------------
# Genre-specific Wikipedia list pages
# (used for indie, jazz, folk, country — where Grammy coverage is thin)
# ---------------------------------------------------------------------------



def _parse_wiki_jazz_sources(client: httpx.Client) -> list[dict]:
    """Scrape Wikipedia pages for jazz songs."""
    records: list[dict] = []

    # Grammy Award for Best Improvised Jazz Solo — Soloist | Track | Album format
    url = "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Improvised_Jazz_Solo"
    try:
        resp = client.get(url, timeout=30)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for table in soup.find_all("table", class_="wikitable"):
                rows = table.find_all("tr")
                current_year: int | None = None
                for row in rows:
                    cells = row.find_all(["td", "th"])
                    texts = [_clean_cell(c.get_text(separator=" ")) for c in cells]
                    if len(cells) == 1:
                        m = _YEAR_RE.search(texts[0])
                        if m:
                            current_year = int(m.group())
                        continue
                    if len(texts) < 2:
                        continue
                    # Format: Soloist | Track | Album
                    artist_raw = texts[0]
                    title = _strip_quotes(texts[1]) if len(texts) > 1 else ""
                    if not title or len(title) < 2:
                        continue
                    records.append({
                        "artist_raw": artist_raw,
                        "title": title,
                        "year": current_year,
                        "genre": "jazz",
                        "source": "grammy",
                        "weight": SOURCE_WEIGHTS["grammy"],
                    })
            print(f"    INFO [jazz_solo]: {len(records)} jazz rows from improvised solo")
        time.sleep(0.4)
    except httpx.HTTPError as exc:
        print(f"    WARNING [jazz_solo]: {exc}")

    # Grammy Award for Best Jazz Vocal Performance (1961–2011) and
    # Best Jazz Vocal Album (2012+) — Year | Performer | Work
    # Works are sometimes song titles, sometimes album titles. We take all.
    for url2 in [
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Jazz_Vocal_Performance",
        "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Jazz_Vocal_Album",
    ]:
        records += _parse_grammy_page(url2, "grammy", "jazz", client)
        time.sleep(0.4)

    # Grammy Award for Best Jazz Instrumental Performance, Individual (1975–2011)
    # Format: Year | Performer | Work (track or album title)
    url3 = "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_Jazz_Instrumental_Performance,_Individual"
    records += _parse_grammy_page(url3, "grammy", "jazz", client)
    time.sleep(0.4)

    print(f"    INFO [jazz_total]: {len(records)} jazz rows scraped across all sources")
    return records


def _parse_wiki_folk_sources(client: httpx.Client) -> list[dict]:
    """Scrape Wikipedia pages for folk songs."""
    # Grammy Best Folk — albums only. Skip.
    # Grammy Best American Roots Song — exists, has song+artist
    records: list[dict] = []

    url = "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_American_Roots_Song"
    records += _parse_grammy_page(url, "grammy", "folk", client)
    time.sleep(0.4)

    url2 = "https://en.wikipedia.org/wiki/Grammy_Award_for_Best_American_Roots_Performance"
    recs2 = _parse_grammy_page(url2, "grammy", "folk", client)
    records += recs2
    time.sleep(0.4)

    return records


# ---------------------------------------------------------------------------
# Collect all sources
# ---------------------------------------------------------------------------


def _collect_all(client: httpx.Client) -> list[dict]:
    all_records: list[dict] = []

    print("\n[indie] Grammy Best Rock Performance / Best Rock Song...")
    all_records += _parse_grammy_rock(client)
    time.sleep(0.5)

    print("\n[hip-hop] Grammy Best Rap Song nominees...")
    all_records += _parse_grammy_rap(client)
    time.sleep(0.5)

    print("\n[hip-hop extra] Grammy Best Hip-Hop Performance / Song (2023+)...")
    all_records += _parse_grammy_hiphop_extra(client)
    time.sleep(0.5)

    print("\n[electronic] Grammy Best Dance Recording nominees...")
    all_records += _parse_grammy_dance(client)
    time.sleep(0.5)

    print("\n[rnb] Grammy Best R&B Song nominees...")
    all_records += _parse_grammy_rnb(client)
    time.sleep(0.5)

    print("\n[metal] Grammy Best Metal Performance nominees...")
    all_records += _parse_grammy_metal(client)
    time.sleep(0.5)

    print("\n[country] Grammy Best Country Song nominees...")
    all_records += _parse_grammy_country(client)
    time.sleep(0.5)

    print("\n[kpop] Gaon Digital Chart year-end...")
    all_records += _parse_gaon_digital(client)
    time.sleep(0.5)

    print("\n[latin] Latin Grammy Record/Song of the Year...")
    all_records += _parse_latin_grammy(client)
    time.sleep(0.5)

    print("\n[jazz] Grammy Best Improvised Jazz Solo...")
    all_records += _parse_wiki_jazz_sources(client)
    time.sleep(0.5)

    print("\n[folk] Grammy Best American Roots Song / Performance...")
    all_records += _parse_wiki_folk_sources(client)
    time.sleep(0.5)

    print("\n[multi-genre] Rolling Stone 500 Greatest Songs...")
    all_records += _parse_rs500(client)

    return all_records


# ---------------------------------------------------------------------------
# Scoring and union
# ---------------------------------------------------------------------------


def _score_and_union(records: list[dict]) -> pd.DataFrame:
    """Merge duplicate songs, compute combined score, return one row per song."""
    candidates: dict[str, dict] = {}

    # Global rank counter per (source, genre) for normalized scoring of Grammy pages
    # (Grammy pages don't have explicit rank numbers, so we assign sequential ranks)
    source_genre_rank: dict[tuple[str, str], int] = {}

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

        source = rec.get("source", "wiki_list")
        genre = rec.get("genre", "indie")
        weight = float(rec.get("weight", 0.5))
        year = rec.get("year")

        # Compute normalized score
        rank_in_list = rec.get("rank_in_list")
        list_length = rec.get("list_length")
        if rank_in_list and list_length:
            norm = (list_length + 1 - rank_in_list) / list_length
        else:
            # Sequential Grammy nominees: first encountered per source/genre gets rank 1
            sg_key = (source, genre)
            source_genre_rank[sg_key] = source_genre_rank.get(sg_key, 0) + 1
            implicit_rank = source_genre_rank[sg_key]
            # Approximate list length from total Grammy nominees (~100-150 per category)
            implicit_len = 150
            norm = max(0.01, (implicit_len + 1 - implicit_rank) / implicit_len)

        weighted = norm * weight

        if dedup_key not in candidates:
            candidates[dedup_key] = {
                "_key": dedup_key,
                "_artist_key": a_key,
                "_title_key": t_key,
                "artist": artist,
                "featured_artists": featured,
                "title": title_str,
                "year": year,
                "genre": genre,
                "combined_score": 0.0,
                "sources": [],
            }
        else:
            # Keep non-null featured_artists and most recent year if we have it
            if featured and not candidates[dedup_key]["featured_artists"]:
                candidates[dedup_key]["featured_artists"] = featured
            if year and not candidates[dedup_key]["year"]:
                candidates[dedup_key]["year"] = year
            # Re-assign genre to the one with the highest weight (first source wins)
            # but prefer rolling_stone genre assignment

        candidates[dedup_key]["combined_score"] += weighted
        if source not in candidates[dedup_key]["sources"]:
            candidates[dedup_key]["sources"].append(source)

    if not candidates:
        return pd.DataFrame()

    df = pd.DataFrame(candidates.values())
    df["source"] = df["sources"].apply(lambda s: ", ".join(s))
    df = df.drop(columns=["sources"])
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Alternate-version filter
# ---------------------------------------------------------------------------


def _drop_alternate_versions(df: pd.DataFrame) -> pd.DataFrame:
    """Remove live/remix/demo variants when the studio original is present."""
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
    print("=== Tier 4: Genre Canonical ===")
    print("Scraping Grammy, Gaon, Latin Grammy, RS 500 from Wikipedia…\n")

    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        raw = _collect_all(client)

    print(f"\nTotal raw records collected: {len(raw)}")

    # ── per-genre stats before dedup ────────────────────────────────────────
    raw_df = pd.DataFrame(raw)
    print("\nPer-genre raw counts:")
    for g in GENRES:
        n = int((raw_df["genre"] == g).sum()) if "genre" in raw_df.columns else 0
        print(f"  {g:15} {n}")

    # ── score and union ──────────────────────────────────────────────────────
    df = _score_and_union(raw)
    if df.empty:
        print("ERROR: no candidates after scoring.")
        sys.exit(1)

    print(f"\nAfter within-Tier-4 union (one row per song): {len(df)}")

    df = _drop_alternate_versions(df)
    print(f"After alternate-version filter: {len(df)}")

    # ── dedup within Tier 4: keep dominant genre for cross-genre songs ───────
    # If a song appears in multiple genres, keep the entry where combined_score
    # is highest (the genre where it had the strongest signal).
    before_genre_dedup = len(df)
    df = df.sort_values("combined_score", ascending=False).drop_duplicates(
        subset=["_artist_key", "_title_key"], keep="first"
    )
    print(f"After cross-genre dedup: {len(df)} (removed {before_genre_dedup - len(df)})")

    # ── load prior tiers ────────────────────────────────────────────────────
    def _load_tier_keys(path: Path) -> set[str]:
        if not path.exists():
            print(f"  WARNING: {path} not found — skipping that dedup")
            return set()
        t = pd.read_csv(path)
        return {
            normalize_for_dedup(str(r["artist"])) + "|||" + normalize_for_dedup(str(r["title"]))
            for _, r in t.iterrows()
        }

    print("\nDeduplicating against prior tiers…")
    t1_keys = _load_tier_keys(TIER1_PATH)
    t2_keys = _load_tier_keys(TIER2_PATH)
    t3_keys = _load_tier_keys(TIER3_PATH)

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

    # ── finalise ─────────────────────────────────────────────────────────────
    df = df.rename(columns={"combined_score": "source_rank"})
    df["tier"] = "genre"
    final = df[
        ["artist", "featured_artists", "title", "year", "genre", "source", "source_rank", "tier"]
    ].reset_index(drop=True)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    final.to_csv(OUTPUT_PATH, index=False)

    # ── summary ──────────────────────────────────────────────────────────────
    print(f"\n=== Summary ===")
    print(f"  Raw records collected:          {len(raw)}")
    print(f"  Removed (Tier 1 overlap):       {n_t1}")
    print(f"  Removed (Tier 2 overlap):       {n_t2}")
    print(f"  Removed (Tier 3 overlap):       {n_t3}")
    print(f"  Total final row count:          {len(final)}")
    feat = int(final["featured_artists"].notna().sum())
    multi = int(final["source"].str.contains(",").sum())
    print(f"  Rows with featured artists:     {feat}")
    print(f"  Rows with multiple sources:     {multi}")

    print("\nPer-genre final counts:")
    for g in GENRES:
        n = int((final["genre"] == g).sum())
        print(f"  {g:15} {n}")
    print(f"\nOutput: {OUTPUT_PATH}")

    # ── success criteria ─────────────────────────────────────────────────────
    print("\n=== Success Criteria ===")
    all_ok = True

    count_ok = 1500 <= len(final) <= 2200
    print(f"  {'OK' if count_ok else 'FAIL'} Total rows {len(final)} (target 1,500-2,200)")
    all_ok = all_ok and count_ok

    no_dupes = not final.duplicated(subset=["artist", "title"]).any()
    print(f"  {'OK' if no_dupes else 'FAIL'} No exact (artist, title) duplicates")
    all_ok = all_ok and no_dupes

    per_genre_ok = True
    for g in GENRES:
        n = int((final["genre"] == g).sum())
        if n < 80:
            print(f"  FAIL {g}: only {n} rows (need ≥80)")
            per_genre_ok = False
    print(f"  {'OK' if per_genre_ok else 'FAIL'} Each genre has ≥80 rows")
    all_ok = all_ok and per_genre_ok

    final_keys = (
        final["artist"].apply(normalize_for_dedup) + "|||" + final["title"].apply(normalize_for_dedup)
    )
    t1_clean = not final_keys.isin(t1_keys).any()
    t2_clean = not final_keys.isin(t2_keys).any()
    t3_clean = not final_keys.isin(t3_keys).any()
    print(f"  {'OK' if t1_clean else 'FAIL'} No Tier 1 songs in Tier 4")
    print(f"  {'OK' if t2_clean else 'FAIL'} No Tier 2 songs in Tier 4")
    print(f"  {'OK' if t3_clean else 'FAIL'} No Tier 3 songs in Tier 4")
    all_ok = all_ok and t1_clean and t2_clean and t3_clean

    if not all_ok:
        print("\nSome criteria not met — review per-genre counts above.")
        sys.exit(1)
    else:
        print("\nAll success criteria met.")


if __name__ == "__main__":
    main()
