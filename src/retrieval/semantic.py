"""Semantic (embedding-based) retrieval with language routing and boost."""

from __future__ import annotations

import psycopg
from FlagEmbedding import BGEM3FlagModel
from lingua import Language, LanguageDetectorBuilder
from sentence_transformers import SentenceTransformer

LANG_BOOST = 0.1

_LANG_TO_ISO: dict[Language, str] = {
    Language.ENGLISH: "en",
    Language.POLISH: "pl",
    Language.GERMAN: "de",
    Language.SPANISH: "es",
}

# Languages served by bge-m3 rather than the English-only bge-base.
MULTILINGUAL_ISO: tuple[str, ...] = ("pl", "de", "es")

_detector = LanguageDetectorBuilder.from_languages(
    Language.ENGLISH, Language.POLISH, Language.GERMAN, Language.SPANISH
).build()

_bge_base: SentenceTransformer | None = None
_bge_m3: BGEM3FlagModel | None = None


def detect_query(query: str) -> tuple[str, str | None]:
    """Return (route, iso_code). Route is 'english' or 'multilingual'."""
    detected = _detector.detect_language_of(query)
    if detected == Language.ENGLISH:
        return "english", "en"
    if detected is None:
        return "english", None
    return "multilingual", _LANG_TO_ISO.get(detected)


def detected_label(query: str) -> str:
    detected = _detector.detect_language_of(query)
    return (
        detected.name.capitalize()
        if detected is not None
        else "Unknown (→ English fallback)"
    )


def _ensure_bge_base() -> SentenceTransformer:
    global _bge_base
    if _bge_base is None:
        print("Loading bge-base-en-v1.5...", flush=True)
        _bge_base = SentenceTransformer("BAAI/bge-base-en-v1.5")
    return _bge_base


def _ensure_bge_m3() -> BGEM3FlagModel:
    global _bge_m3
    if _bge_m3 is None:
        print("Loading bge-m3...", flush=True)
        _bge_m3 = BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)
    return _bge_m3


def semantic_search(
    conn: psycopg.Connection,
    query: str,
    query_lang: str | None,
    top_k: int = 100,
) -> list[tuple[int, float]]:
    """Return [(passage_id, similarity), ...] sorted by similarity desc."""
    if query_lang in MULTILINGUAL_ISO:
        model = _ensure_bge_m3()
        vec = model.encode([query], batch_size=1, max_length=512)["dense_vecs"][
            0
        ].tolist()
        column = "embedding_multi"
    else:
        model = _ensure_bge_base()
        vec = model.encode(query, normalize_embeddings=True).tolist()
        column = "embedding"

    rows = conn.execute(
        f"""
        SELECT p.id,
               1 - (p.{column} <=> %s::vector)
               + CASE WHEN p.language = %s THEN {LANG_BOOST} ELSE 0.0 END AS sim
        FROM passages p
        ORDER BY sim DESC
        LIMIT %s
        """,
        (vec, query_lang, top_k),
    ).fetchall()

    return [(row[0], float(row[1])) for row in rows]
