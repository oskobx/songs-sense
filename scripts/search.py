"""Language-aware lyric search with per-mode retrieval profiles.

Usage:
    python scripts/search.py "feeling lost at night"
    python scripts/search.py --mode find "shorty had them apple bottom jeans"
    python scripts/search.py --mode twin "I miss the days when we were young"
    python scripts/search.py --top 5 --mode find "your query here"

Modes:
    vibe  (default) — semantic only; pure embedding match + language boost
    find            — hybrid (semantic + BM25 via RRF); best for lyric snippets
    twin            — semantic only; same as vibe, MMR diversity coming in Phase 5

Routing:
    English queries   → bge-base-en-v1.5  (embedding column)
    Polish / German   → bge-m3 dense      (embedding_multi column)
    Ambiguous / None  → English fallback
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.retrieval.bm25 import bm25_search
from src.retrieval.hybrid import rrf_combine
from src.retrieval.semantic import (
    LANG_BOOST,
    detect_query,
    detected_label,
    semantic_search,
)

load_dotenv()


def _fetch_passages(
    conn: psycopg.Connection, passage_ids: list[int]
) -> list[dict]:
    if not passage_ids:
        return []
    rows = conn.execute(
        """
        SELECT s.artist, s.title, s.year, s.tier, p.language, p.passage_text, p.id
        FROM passages p
        JOIN songs s ON p.song_id = s.id
        WHERE p.id = ANY(%s)
        """,
        (passage_ids,),
    ).fetchall()
    return [
        {
            "artist": r[0],
            "title": r[1],
            "year": r[2],
            "tier": r[3],
            "language": r[4],
            "passage_text": r[5],
            "id": r[6],
        }
        for r in rows
    ]


def _snippet(text: str) -> str:
    return text[:200].replace("\n", " / ")


def _dominant(passage_id: int, sem_ids: set[int], bm25_ids: set[int]) -> str:
    in_sem = passage_id in sem_ids
    in_bm25 = passage_id in bm25_ids
    if in_sem and in_bm25:
        return "both"
    if in_sem:
        return "semantic"
    return "BM25"


def run_query(
    query: str,
    mode: str,
    top_k: int,
    conn: psycopg.Connection,
) -> None:
    route, query_lang = detect_query(query)
    lang_label = detected_label(query)
    boost_note = (
        f"+{LANG_BOOST} boost for '{query_lang}' passages"
        if query_lang
        else "no boost (unknown lang)"
    )
    model_label = (
        "bge-m3 (embedding_multi)" if query_lang in ("pl", "de")
        else "bge-base-en-v1.5 (embedding)"
    )

    print(f"Query    : {query!r}")
    print(f"Mode     : {mode}")
    print(f"Detected : {lang_label}  ({boost_note})")

    if mode in ("vibe", "twin"):
        print(f"Profile  : semantic only  [{model_label}]")
        print()

        sem_results = semantic_search(conn, query, query_lang, top_k=top_k)
        sem_scores = dict(sem_results)
        rows = _fetch_passages(conn, [pid for pid, _ in sem_results])
        rows.sort(key=lambda r: sem_scores.get(r["id"], 0.0), reverse=True)

        for i, row in enumerate(rows, 1):
            sim = sem_scores.get(row["id"], 0.0)
            print(
                f"  {i:2}. [sem={sim:.4f}] [dominant=semantic]"
                f"  {row['artist']} — {row['title']}"
                f" ({row['year']}, {row['tier']}) [{row['language'] or '??':>2s}]"
            )
            print(f"       {_snippet(row['passage_text'])}")
        print()

    elif mode == "find":
        print(f"Profile  : hybrid (semantic + BM25 via RRF, k=60)  [{model_label}]")
        print()

        sem_results = semantic_search(conn, query, query_lang, top_k=100)
        bm25_results = bm25_search(conn, query, top_k=100)
        rrf_results = rrf_combine([sem_results, bm25_results], k=60, top_n=top_k)

        sem_scores = dict(sem_results)
        bm25_scores = dict(bm25_results)
        rrf_scores = dict(rrf_results)
        sem_ids = set(sem_scores)
        bm25_ids = set(bm25_scores)

        rows = _fetch_passages(conn, [pid for pid, _ in rrf_results])
        rows.sort(key=lambda r: rrf_scores.get(r["id"], 0.0), reverse=True)

        for i, row in enumerate(rows, 1):
            pid = row["id"]
            rrf = rrf_scores.get(pid, 0.0)
            sem = sem_scores.get(pid)
            bm25 = bm25_scores.get(pid)
            dom = _dominant(pid, sem_ids, bm25_ids)

            sem_str = f"{sem:.4f}" if sem is not None else "  —   "
            bm25_str = f"{bm25:.5f}" if bm25 is not None else "   —    "

            print(
                f"  {i:2}. [rrf={rrf:.5f}] [sem={sem_str}] [bm25={bm25_str}]"
                f" [dominant={dom}]"
            )
            print(
                f"       {row['artist']} — {row['title']}"
                f" ({row['year']}, {row['tier']}) [{row['language'] or '??':>2s}]"
            )
            print(f"       {_snippet(row['passage_text'])}")
        print()

    else:
        print(f"Unknown mode: {mode!r}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Language-aware lyric search")
    parser.add_argument("query", nargs="?", default=None)
    parser.add_argument("--top", type=int, default=10, metavar="K")
    parser.add_argument(
        "--mode",
        choices=["vibe", "find", "twin"],
        default="vibe",
        help="Retrieval profile: vibe=semantic, find=hybrid, twin=semantic (default: vibe)",
    )
    args = parser.parse_args()

    url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(url)
    register_vector(conn)

    queries = [args.query] if args.query else []

    if queries:
        for q in queries:
            run_query(q, args.mode, args.top, conn)
    else:
        print(f"songs-sense search  [mode={args.mode}]  (empty line or Ctrl+D to quit)\n")
        while True:
            try:
                q = input(">>> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q:
                break
            run_query(q, args.mode, args.top, conn)

    conn.close()


if __name__ == "__main__":
    main()
