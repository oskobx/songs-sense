"""Substep 2b: Embed all passages with bge-base-en-v1.5 and store in DB.

Usage:
    python -m src.embeddings.embed_passages              # test 100, prompt to continue
    python -m src.embeddings.embed_passages --skip-test  # full run, no prompt
    python -m src.embeddings.embed_passages --index-only # skip embedding, build HNSW index
    python -m src.embeddings.embed_passages --sanity     # skip embedding/index, run sanity query

Environment:
    Set PYTORCH_ENABLE_MPS_FALLBACK=1 to handle MPS ops not yet supported on Metal.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np
import psycopg
import torch
from dotenv import load_dotenv
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

load_dotenv()

MODEL_NAME = "BAAI/bge-base-en-v1.5"
ENCODE_BATCH = 32
DB_BATCH = 1000
HNSW_M = 16
HNSW_EF = 64


# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------

def pick_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def load_model(device: str) -> SentenceTransformer:
    print(f"Loading {MODEL_NAME} on {device.upper()}...")
    model = SentenceTransformer(MODEL_NAME, device=device)
    print("Model loaded.")
    return model


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------

def encode_texts(model: SentenceTransformer, texts: list[str]) -> np.ndarray:
    return model.encode(
        texts,
        batch_size=ENCODE_BATCH,
        normalize_embeddings=True,
        show_progress_bar=False,
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_conn(url: str) -> psycopg.Connection:
    conn = psycopg.connect(url)
    register_vector(conn)
    return conn


def fetch_null_batch(conn: psycopg.Connection, limit: int) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT id, passage_text FROM passages WHERE embedding IS NULL LIMIT %s",
        (limit,),
    ).fetchall()
    return rows


def write_embeddings(
    conn: psycopg.Connection,
    ids: list[int],
    vecs: np.ndarray,
) -> None:
    with conn.pipeline():
        for pid, vec in zip(ids, vecs):
            conn.execute(
                "UPDATE passages SET embedding = %s WHERE id = %s",
                (vec, pid),
            )
    conn.commit()


def count_null(conn: psycopg.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM passages WHERE embedding IS NULL"
    ).fetchone()[0]


def count_total(conn: psycopg.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0]


# ---------------------------------------------------------------------------
# Test run (first 100 passages)
# ---------------------------------------------------------------------------

def run_test(model: SentenceTransformer, conn: psycopg.Connection) -> float:
    """Embed 100 passages, print diagnostics, return passages/sec throughput."""
    print("\n--- Test run: first 100 passages ---")
    rows = conn.execute(
        "SELECT id, passage_text FROM passages WHERE embedding IS NULL LIMIT 100"
    ).fetchall()

    if not rows:
        print("No NULL embeddings found — already fully embedded.")
        return 0.0

    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]

    t0 = time.perf_counter()
    vecs = encode_texts(model, texts)
    elapsed = time.perf_counter() - t0

    per_passage = elapsed / len(texts)
    throughput = len(texts) / elapsed

    print(f"  Passages encoded : {len(texts)}")
    print(f"  Total time       : {elapsed:.2f}s")
    print(f"  Per passage      : {per_passage*1000:.1f}ms")
    print(f"  Throughput       : {throughput:.1f} passages/sec")
    print(f"  Sample embedding (first 5 dims): {vecs[0][:5].tolist()}")
    print(f"  Avg norm         : {np.linalg.norm(vecs, axis=1).mean():.6f}")

    return throughput


def estimate_runtime(throughput: float, null_count: int) -> None:
    if throughput <= 0:
        return
    remaining_sec = null_count / throughput
    print(f"\n  Null passages remaining : {null_count:,}")
    print(f"  Estimated runtime       : {remaining_sec/60:.1f} min  ({remaining_sec:.0f}s)")
    if remaining_sec > 3600:
        print(f"                          = {remaining_sec/3600:.1f} hours")


# ---------------------------------------------------------------------------
# Full embedding pass
# ---------------------------------------------------------------------------

def embed_all_passages(model: SentenceTransformer, conn: psycopg.Connection) -> None:
    total_start = time.perf_counter()
    processed = 0

    while True:
        rows = fetch_null_batch(conn, DB_BATCH)
        if not rows:
            break

        ids = [r[0] for r in rows]
        texts = [r[1] for r in rows]

        vecs = encode_texts(model, texts)
        write_embeddings(conn, ids, vecs)

        processed += len(rows)
        elapsed = time.perf_counter() - total_start
        throughput = processed / elapsed
        remaining = count_null(conn)
        eta_sec = remaining / throughput if throughput > 0 else 0
        print(
            f"  Embedded {processed:>6,} | remaining {remaining:>6,} | "
            f"{throughput:.1f} p/s | ETA {eta_sec/60:.1f}min"
        )

    total_elapsed = time.perf_counter() - total_start
    total_passages = count_total(conn)
    print(f"\n  Embedding complete: {processed:,} passages in {total_elapsed:.1f}s "
          f"({processed/total_elapsed:.1f} p/s)")

    # Sample 5 random passages for spot-check
    sample_rows = conn.execute(
        "SELECT id, passage_text, embedding FROM passages "
        "WHERE embedding IS NOT NULL ORDER BY random() LIMIT 5"
    ).fetchall()
    norms = [np.linalg.norm(np.array(r[2])) for r in sample_rows]
    print(f"  Avg norm (sample) : {np.mean(norms):.6f}  (should be ~1.0)")
    for row in sample_rows:
        vec = np.array(row[2])
        print(f"    id={row[0]}  embedding[:5]={vec[:5].tolist()}")
        print(f"           passage: {row[1][:80]!r}")


# ---------------------------------------------------------------------------
# HNSW index
# ---------------------------------------------------------------------------

def create_hnsw_index(conn: psycopg.Connection) -> None:
    print("\n--- Building HNSW index ---")
    t0 = time.perf_counter()
    conn.execute("SET statement_timeout = 0")
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS passages_embedding_hnsw_idx
        ON passages
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = {HNSW_M}, ef_construction = {HNSW_EF})
        """
    )
    conn.commit()
    elapsed = time.perf_counter() - t0
    print(f"  HNSW index built in {elapsed:.1f}s")


# ---------------------------------------------------------------------------
# Sanity query
# ---------------------------------------------------------------------------

def run_sanity(model: SentenceTransformer, conn: psycopg.Connection) -> None:
    query = "feeling lost at night"
    print(f"\n--- Sanity test: '{query}' ---")
    vec = encode_texts(model, [query])[0]

    rows = conn.execute(
        """
        SELECT p.id, s.artist, s.title, p.passage_text,
               1 - (p.embedding <=> %s::vector) AS cosine_sim
        FROM passages p
        JOIN songs s ON s.id = p.song_id
        ORDER BY p.embedding <=> %s::vector
        LIMIT 5
        """,
        (vec.tolist(), vec.tolist()),
    ).fetchall()

    print(f"  Top-5 passages for '{query}':\n")
    for rank, (pid, artist, title, text, sim) in enumerate(rows, 1):
        print(f"  [{rank}] {artist} — {title}  (id={pid}, sim={sim:.4f})")
        print(f"       {text[:120]!r}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Embed passages with bge-base-en-v1.5")
    parser.add_argument("--skip-test", action="store_true",
                        help="Skip the 100-passage test and go straight to full run")
    parser.add_argument("--index-only", action="store_true",
                        help="Skip embedding, just build HNSW index")
    parser.add_argument("--sanity", action="store_true",
                        help="Skip embedding and index, run sanity query only")
    args = parser.parse_args()

    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

    url = os.environ["DATABASE_URL"]

    device = pick_device()
    print(f"Device: {device.upper()}")

    conn = get_conn(url)

    if args.sanity:
        model = load_model(device)
        run_sanity(model, conn)
        conn.close()
        return

    if args.index_only:
        create_hnsw_index(conn)
        conn.close()
        return

    model = load_model(device)

    if not args.skip_test:
        null_before = count_null(conn)
        throughput = run_test(model, conn)
        estimate_runtime(throughput, null_before)

        answer = input("\nContinue with full embedding pass? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted. Re-run with --skip-test to bypass this prompt.")
            conn.close()
            return

    embed_all_passages(model, conn)
    create_hnsw_index(conn)
    run_sanity(model, conn)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
