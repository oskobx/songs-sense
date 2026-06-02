"""Quick semantic search against the lyrics corpus."""

import os
import sys
import psycopg
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

load_dotenv()

QUERY = sys.argv[1] if len(sys.argv) > 1 else "feeling lost at night"

model = SentenceTransformer("BAAI/bge-base-en-v1.5")
embedding = model.encode(QUERY, normalize_embeddings=True).tolist()

with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    rows = conn.execute(
        """
        SELECT s.artist, s.title, s.year, s.tier,
               1 - (p.embedding <=> %s::vector) AS sim,
               p.passage_text
        FROM passages p
        JOIN songs s ON p.song_id = s.id
        ORDER BY p.embedding <=> %s::vector
        LIMIT 10
        """,
        (embedding, embedding),
    ).fetchall()

    print(f"Query: {QUERY!r}\n")
    for artist, title, year, tier, sim, text in rows:
        print(f"[{sim:.3f}] {artist} — {title} ({year}, {tier})")
        snippet = text[:200].replace("\n", " / ")
        print(f"         {snippet}\n")