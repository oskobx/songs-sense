"""FastAPI app exposing Vibe Search.

Retrieval logic is imported, never reimplemented: language detection, model
routing and the language boost all come from src/retrieval/semantic.py. What
lives here is presentation — clamping k, aggregating passages up to songs, and
shaping the JSON.

Handlers are sync `def` rather than `async def` on purpose. Both the embedding
models and psycopg are blocking, so an async handler would stall the event loop
for the whole query; a sync handler is run in FastAPI's threadpool instead.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pgvector.psycopg import register_vector
from pydantic import BaseModel, Field

from src.retrieval.semantic import (
    MULTILINGUAL_ISO,
    detect_query,
    semantic_search,
)

# Private in semantic.py, but they are the only way to warm the models without
# issuing a throwaway query. Importing them beats adding a loader to that module.
from src.retrieval.semantic import _ensure_bge_base, _ensure_bge_m3  # noqa: E402

logger = logging.getLogger("songs_sense.api")

BGE_BASE = "BAAI/bge-base-en-v1.5"
BGE_M3 = "BAAI/bge-m3"

K_MIN, K_MAX, K_DEFAULT = 1, 25, 10

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
INDEX_HTML = STATIC_DIR / "index.html"


def embed_multilingual_enabled() -> bool:
    """EMBED_MULTILINGUAL=false keeps bge-m3 (~1 GB) out of memory in production."""
    raw = os.environ.get("EMBED_MULTILINGUAL", "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class SearchRequest(BaseModel):
    query: str
    k: int = Field(default=K_DEFAULT)


class SearchResult(BaseModel):
    rank: int
    artist: str
    title: str
    year: int | None
    passage: str
    score: float


class SearchResponse(BaseModel):
    query: str
    detected_language: str
    results: list[SearchResult]


class HealthResponse(BaseModel):
    status: str
    models: list[str]


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #


def _routing_language(detected: str | None) -> str | None:
    """Language to hand the retriever, honouring the English-only setting.

    With multilingual embeddings off, a pl/de/es query must not reach bge-m3.
    It is routed to bge-base with no boost rather than boosted toward English,
    which would actively favour the wrong passages. The detected language is
    still reported to the caller either way.
    """
    if detected is None:
        return None
    if embed_multilingual_enabled():
        return detected
    return None if detected in MULTILINGUAL_ISO else detected


def _connect() -> psycopg.Connection:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg.connect(url)
    register_vector(conn)
    return conn


def vibe_search(query: str, k: int) -> tuple[str, list[SearchResult]]:
    """Return (detected_language, top-k songs) for a vibe query."""
    detected = detect_query(query)[1]
    routing_lang = _routing_language(detected)

    # Over-fetch passages: several of the top hits are usually different chunks
    # of the same song, and we need k *distinct* songs after aggregation.
    depth = min(max(k * 4, 40), 200)

    with _connect() as conn:
        scored = semantic_search(conn, query, routing_lang, top_k=depth)
        if not scored:
            return detected or "unknown", []

        order = {pid: i for i, (pid, _) in enumerate(scored)}
        rows = conn.execute(
            """
            SELECT p.id, s.artist, s.title, s.year, p.passage_text
            FROM passages p
            JOIN songs s ON p.song_id = s.id
            WHERE p.id = ANY(%s)
            """,
            ([pid for pid, _ in scored],),
        ).fetchall()

    score_by_id = dict(scored)
    rows.sort(key=lambda r: order.get(r[0], len(order)))

    # One row per song, keeping its best-scoring passage. songs has a
    # UNIQUE(artist, title) constraint, so that pair identifies a song.
    best: dict[tuple[str, str], SearchResult] = {}
    for passage_id, artist, title, year, passage_text in rows:
        key = (artist, title)
        if key in best:
            continue
        best[key] = SearchResult(
            rank=0,
            artist=artist,
            title=title,
            year=year,
            passage=passage_text.strip(),
            score=float(score_by_id.get(passage_id, 0.0)),
        )
        if len(best) >= k:
            break

    results = list(best.values())
    for rank, result in enumerate(results, 1):
        result.rank = rank
    return detected or "unknown", results


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    multilingual = embed_multilingual_enabled()
    loaded: list[str] = []

    started = time.monotonic()
    _ensure_bge_base()
    loaded.append(BGE_BASE)
    logger.info("loaded %s in %.1fs", BGE_BASE, time.monotonic() - started)

    if multilingual:
        started = time.monotonic()
        _ensure_bge_m3()
        loaded.append(BGE_M3)
        logger.info("loaded %s in %.1fs", BGE_M3, time.monotonic() - started)
    else:
        logger.info(
            "EMBED_MULTILINGUAL=false — skipping %s, English-only routing", BGE_M3
        )

    started = time.monotonic()
    try:
        with _connect() as conn:
            passages = conn.execute("SELECT count(*) FROM passages").fetchone()[0]
        logger.info(
            "database ready in %.2fs (%s passages)",
            time.monotonic() - started,
            passages,
        )
    except Exception as exc:  # a dead DB should be loud at boot, not on first query
        logger.error("database unavailable at startup: %s", exc)

    app.state.models = loaded
    yield


app = FastAPI(title="songs-sense", version="0.1.0", lifespan=lifespan)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", models=getattr(app.state, "models", []))


@app.post("/search/vibe", response_model=SearchResponse)
def search_vibe(request: SearchRequest) -> SearchResponse:
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query must not be empty")

    k = max(K_MIN, min(request.k, K_MAX))
    detected, results = vibe_search(query, k)
    return SearchResponse(query=query, detected_language=detected, results=results)


@app.get("/")
def index():
    if not INDEX_HTML.is_file():
        raise HTTPException(status_code=404, detail="static/index.html not built yet")
    return FileResponse(INDEX_HTML)
