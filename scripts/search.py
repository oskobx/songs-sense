"""Language-aware semantic search against the lyrics corpus.

Usage:
    python scripts/search.py "feeling lost at night"
    python scripts/search.py "samotność w mieście"
    python scripts/search.py "die Nacht ist dunkel"
    python scripts/search.py --top 5 "your query here"

Routing:
    English queries   → bge-base-en-v1.5  (embedding column)
    Polish / German   → bge-m3 dense      (embedding_multi column)
    Ambiguous / None  → English fallback

Boost:
    Passages whose language matches the query language get +0.1 similarity.
"""

from __future__ import annotations

import argparse
import os

import psycopg
from dotenv import load_dotenv
from FlagEmbedding import BGEM3FlagModel
from lingua import Language, LanguageDetectorBuilder
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

load_dotenv()

LANG_BOOST = 0.1

# ISO codes for the three routable languages
_LANG_TO_ISO = {
    Language.ENGLISH: "en",
    Language.POLISH: "pl",
    Language.GERMAN: "de",
}

# ---------------------------------------------------------------------------
# Language detector (restricted to the three priority languages)
# ---------------------------------------------------------------------------

_detector = (
    LanguageDetectorBuilder
    .from_languages(Language.ENGLISH, Language.POLISH, Language.GERMAN)
    .build()
)


def detect_query(query: str) -> tuple[str, str | None]:
    """Return (route, iso_code): route is 'english' or 'multilingual',
    iso_code is 'en'/'pl'/'de' or None if undetected."""
    detected = _detector.detect_language_of(query)
    if detected == Language.ENGLISH:
        return "english", "en"
    if detected is None:
        return "english", None
    return "multilingual", _LANG_TO_ISO.get(detected)


def detected_label(query: str) -> str:
    detected = _detector.detect_language_of(query)
    return detected.name.capitalize() if detected is not None else "Unknown (→ English fallback)"


# ---------------------------------------------------------------------------
# Model loading (both stay in memory for fast switching)
# ---------------------------------------------------------------------------

def load_models() -> tuple[SentenceTransformer, BGEM3FlagModel]:
    print("Loading bge-base-en-v1.5...", flush=True)
    bge_base = SentenceTransformer("BAAI/bge-base-en-v1.5")
    print("Loading bge-m3...", flush=True)
    bge_m3 = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)
    print("Models ready.\n", flush=True)
    return bge_base, bge_m3


# ---------------------------------------------------------------------------
# Query encoding
# ---------------------------------------------------------------------------

def encode_query_english(model: SentenceTransformer, query: str) -> list[float]:
    return model.encode(query, normalize_embeddings=True).tolist()


def encode_query_multi(model: BGEM3FlagModel, query: str) -> list[float]:
    vec = model.encode([query], batch_size=1, max_length=512)["dense_vecs"][0]
    return vec.tolist()


# ---------------------------------------------------------------------------
# Search (with language boost)
# ---------------------------------------------------------------------------

def search(
    conn: psycopg.Connection,
    vec: list[float],
    column: str,
    query_lang: str | None,
    top_k: int,
) -> list[tuple]:
    return conn.execute(
        f"""
        SELECT s.artist, s.title, s.year, s.tier,
               1 - (p.{column} <=> %s::vector)
               + CASE WHEN p.language = %s THEN {LANG_BOOST} ELSE 0.0 END AS sim,
               p.passage_text,
               p.language
        FROM passages p
        JOIN songs s ON p.song_id = s.id
        ORDER BY sim DESC
        LIMIT %s
        """,
        (vec, query_lang, top_k),
    ).fetchall()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Language-aware lyric search")
    parser.add_argument("query", nargs="?", default="feeling lost at night")
    parser.add_argument("--top", type=int, default=10, metavar="K")
    parser.add_argument(
        "--sanity",
        action="store_true",
        help="Run all three sanity queries (English / Polish / German)",
    )
    args = parser.parse_args()

    bge_base, bge_m3 = load_models()

    url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(url)
    register_vector(conn)

    queries = (
        ["feeling lost at night", "samotność w mieście", "die Nacht ist dunkel"]
        if args.sanity
        else [args.query]
    )

    for q in queries:
        route, query_lang = detect_query(q)
        lang_label = detected_label(q)

        if route == "english":
            vec = encode_query_english(bge_base, q)
            column = "embedding"
            model_label = "bge-base-en-v1.5"
        else:
            vec = encode_query_multi(bge_m3, q)
            column = "embedding_multi"
            model_label = "bge-m3"

        rows = search(conn, vec, column, query_lang, args.top)

        boost_note = f"+{LANG_BOOST} boost for '{query_lang}' passages" if query_lang else "no boost (unknown lang)"
        print(f"Query    : {q!r}")
        print(f"Detected : {lang_label}  ({boost_note})")
        print(f"Model    : {model_label}  (column: {column})")
        print()
        for artist, title, year, tier, sim, text, plang in rows:
            snippet = text[:200].replace("\n", " / ")
            print(f"  [{sim:.3f}] [{plang or '??':>2s}] {artist} — {title} ({year}, {tier})")
            print(f"           {snippet}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
