"""Shared string-cleaning utilities for all corpus tiers."""

from __future__ import annotations

import re


def normalize_quotes(s: str) -> str:
    """Replace curly/smart quotes with straight ASCII equivalents."""
    return (
        s.replace("‘", "'")
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
    )


def smart_title_case(s: str) -> str:
    """Title-case a string without mis-capitalizing after apostrophes.

    Converts 'DON'T STOP ME NOW' → "Don't Stop Me Now" (not "Don'T Stop Me Now").
    """
    result = s.title()
    # Fix apostrophe: str.title() capitalizes the letter after every ', undo that
    result = re.sub(r"(?<=[A-Za-z])'([A-Z])", lambda m: "'" + m.group(1).lower(), result)
    return result


def _split_featured_list(s: str) -> str:
    """Normalize a raw featured-artist string to a comma-separated list.

    'Kid Cudi & Lloyd' → 'Kid Cudi, Lloyd'
    'Drake and Lil Baby' → 'Drake, Lil Baby'
    'Lauren Bennett & GoonRock' → 'Lauren Bennett, GoonRock'
    """
    parts = re.split(r"\s*(?:[&,]|\band\b)\s*", s)
    parts = [p.strip() for p in parts if p.strip()]
    return ", ".join(parts)


def _normalize_band_key(s: str) -> str:
    """Strip commas, &, and extra whitespace for band-allowlist lookup."""
    s = s.lower()
    s = re.sub(r"[,&]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Band names that contain commas and must never be split by the comma heuristic.
_KNOWN_BANDS: frozenset[str] = frozenset(
    _normalize_band_key(name)
    for name in [
        "Earth, Wind & Fire",
        "Crosby, Stills & Nash",
        "Crosby, Stills, Nash & Young",
        "Emerson, Lake & Palmer",
        "Blood, Sweat & Tears",
        "Peter, Paul & Mary",
    ]
)


def parse_featured_artists(raw_artist: str) -> tuple[str, str | None]:
    """Split a raw artist string into (primary_artist, featured_artists_or_None).

    Handles:
      "Drake (feat. Kid Cudi & Lloyd)"   → ("Drake", "Kid Cudi, Lloyd")
      "Lil Nas X feat. Billy Ray Cyrus"  → ("Lil Nas X", "Billy Ray Cyrus")
      "Santana Featuring Rob Thomas"     → ("Santana", "Rob Thomas")
      "Drake, Kid Cudi, Lloyd"           → ("Drake", "Kid Cudi, Lloyd")
      "Simon & Garfunkel"                → ("Simon & Garfunkel", None)
      "Earth, Wind & Fire"               → ("Earth, Wind & Fire", None)
      "AC/DC"                            → ("AC/DC", None)
    """
    raw = normalize_quotes(raw_artist).strip()

    # Pattern 1: parenthetical — "Artist (feat. X & Y)"
    m = re.match(
        r"^(.+?)\s*\(\s*(?:feat\.?|ft\.?|featuring|w/|with)\s+(.+?)\s*\)\s*$",
        raw,
        re.IGNORECASE,
    )
    if m:
        primary = m.group(1).strip()
        featured = _split_featured_list(m.group(2))
        return primary, featured or None

    # Pattern 2: inline — "Artist feat. X, Y" or "Artist featuring X"
    m = re.match(
        r"^(.+?)\s+(?:feat\.?|ft\.?|featuring|w/|with)\s+(.+)$",
        raw,
        re.IGNORECASE,
    )
    if m:
        primary = m.group(1).strip()
        featured = _split_featured_list(m.group(2))
        return primary, featured or None

    # Guard: known band names with commas must not be split by the heuristic below.
    if _normalize_band_key(raw) in _KNOWN_BANDS:
        return raw, None

    # Pattern 3: comma-separated list — first name is primary, rest are features.
    # "Drake, Kid Cudi, Lloyd" → ("Drake", "Kid Cudi, Lloyd")
    # Intentionally NOT splitting plain "X & Y" (= band name like Simon & Garfunkel).
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) > 1:
        primary = parts[0]
        featured = _split_featured_list(", ".join(parts[1:]))
        return primary, featured or None

    # No recognizable separator → single artist or band name with &
    return raw, None


def normalize_for_dedup(text: str) -> str:
    """Lowercase + strip 'The ' prefix + strip punctuation, for dedup key only.

    'The Beatles' and 'Beatles' compare equal.
    'Bohemian Rhapsody' and 'Bohemian Rhapsody!' compare equal.
    Storage values are never passed through this function.
    """
    s = text.lower().strip()
    if s.startswith("the "):
        s = s[4:]
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s
