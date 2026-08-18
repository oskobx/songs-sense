"""Run Vibe Search retrieval for eval, reusing src/retrieval/semantic.py verbatim.

Vibe mode is semantic-only (see scripts/search.py): detect the query language,
route to bge-base or bge-m3, apply the language boost, take the top k. This
module adds nothing to that — it only opens a connection, calls
`semantic_search`, and joins passage rows to song metadata so the judge has an
artist and title to show. Any change to ranking belongs in src/retrieval/, not
here, or the eval stops measuring the real system.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

from src.retrieval.semantic import LANG_BOOST, detect_query, semantic_search

load_dotenv()

RETRIEVAL_MODE = "vibe"


@dataclass(frozen=True)
class RetrievedPassage:
    rank: int  # 1-indexed
    passage_id: int
    score: float
    artist: str
    title: str
    year: int | None
    language: str | None
    passage_text: str


@contextmanager
def db_connection() -> Iterator[psycopg.Connection]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set in .env")
    conn = psycopg.connect(url)
    try:
        register_vector(conn)
        yield conn
    finally:
        conn.close()


def _fetch_passage_rows(
    conn: psycopg.Connection, passage_ids: list[int]
) -> dict[int, dict]:
    if not passage_ids:
        return {}
    rows = conn.execute(
        """
        SELECT p.id, s.artist, s.title, s.year, p.language, p.passage_text
        FROM passages p
        JOIN songs s ON p.song_id = s.id
        WHERE p.id = ANY(%s)
        """,
        (passage_ids,),
    ).fetchall()
    return {
        row[0]: {
            "artist": row[1],
            "title": row[2],
            "year": row[3],
            "language": row[4],
            "passage_text": row[5],
        }
        for row in rows
    }


def retrieve_vibe(
    conn: psycopg.Connection, query: str, top_k: int = 10
) -> tuple[list[RetrievedPassage], str | None]:
    """Return (top-k results, detected query language).

    The detected language is returned alongside because it drives both the model
    route and the boost — when it disagrees with the query's declared language,
    that is a retrieval finding worth recording, not something to paper over.
    """
    _, detected_lang = detect_query(query)
    scored = semantic_search(conn, query, detected_lang, top_k=top_k)
    rows = _fetch_passage_rows(conn, [pid for pid, _ in scored])

    results: list[RetrievedPassage] = []
    for rank, (passage_id, score) in enumerate(scored, 1):
        row = rows.get(passage_id)
        if (
            row is None
        ):  # passage deleted between search and fetch; skip rather than crash
            continue
        results.append(
            RetrievedPassage(
                rank=rank,
                passage_id=passage_id,
                score=float(score),
                artist=row["artist"],
                title=row["title"],
                year=row["year"],
                language=row["language"],
                passage_text=row["passage_text"],
            )
        )
    return results, detected_lang


def config_description(note: str | None = None) -> str:
    """One-line description of the retrieval config, recorded in every results file.

    Comparing runs across changes is guesswork without it.
    """
    base = (
        f"mode={RETRIEVAL_MODE} semantic-only | "
        f"en=bge-base-en-v1.5, pl/de=bge-m3 (dense) | "
        f"language boost +{LANG_BOOST} | no reranker"
    )
    return f"{base} | {note}" if note else base
