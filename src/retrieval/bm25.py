"""BM25 retrieval via Postgres full-text search (tsvector + ts_rank_cd).

Uses a *disjunctive* (OR) tsquery so partial lyric matches still score.
plainto_tsquery applies stemming + stopword removal; we convert the conjunctive
(&) form to disjunctive (|) so passages that share some — but not all — query
terms are still found and ranked by cover-density.

This diverges from the spec's AND example but is more appropriate for the
Find-the-Song use case where users type half-remembered lyrics.
"""

from __future__ import annotations

import psycopg


def bm25_search(
    conn: psycopg.Connection,
    query: str,
    top_k: int = 100,
) -> list[tuple[int, float]]:
    """Return [(passage_id, ts_rank), ...] sorted by ts_rank desc."""
    # Stem + remove stopwords via plainto_tsquery, then convert AND→OR
    tsq_text: str | None = conn.execute(
        "SELECT plainto_tsquery('english', %s)::text", (query,)
    ).fetchone()[0]

    if not tsq_text:
        return []

    or_tsq = tsq_text.replace(" & ", " | ")

    rows = conn.execute(
        """
        SELECT id,
               ts_rank_cd(passage_tsv, %s::tsquery) AS rank
        FROM passages
        WHERE passage_tsv @@ %s::tsquery
        ORDER BY rank DESC
        LIMIT %s
        """,
        (or_tsq, or_tsq, top_k),
    ).fetchall()

    return [(row[0], float(row[1])) for row in rows]
