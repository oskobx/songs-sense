"""Shared lyrics cleaning applied before storing in Postgres."""

from __future__ import annotations

import re


# Matches contributor credit artifacts from Genius scrapes:
# e.g. "123 ContributorsTranslationsFrançaisLyrics"
_CONTRIBUTOR_RE = re.compile(r"^\d+\s+Contributors.*?Lyrics\s*", re.DOTALL)

# Section headers like [Verse 1], [Chorus], [Pre-Chorus], [Bridge]
_SECTION_HEADER_RE = re.compile(r"^\[.+?\]\s*$", re.MULTILINE)

# Three or more consecutive newlines → two newlines (preserve verse breaks)
_EXCESS_NEWLINES_RE = re.compile(r"\n{3,}")


def clean_lyrics(raw: str) -> str:
    """Return cleaned lyric text ready for Postgres storage.

    Applies:
    - Strip leading/trailing whitespace
    - Remove Genius contributor credits (e.g. "123 ContributorsLyrics")
    - Remove section headers like [Verse 1], [Chorus], [Bridge]
    - Collapse 3+ consecutive newlines into 2
    """
    text = raw.strip()
    text = _CONTRIBUTOR_RE.sub("", text)
    text = _SECTION_HEADER_RE.sub("", text)
    text = _EXCESS_NEWLINES_RE.sub("\n\n", text)
    return text.strip()
