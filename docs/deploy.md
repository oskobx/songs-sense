# Deployment — Neon + Render

Manual steps, in order. Nothing here is automated; run it once and note anything
that drifts.

Two moving parts: Postgres with pgvector on **Neon**, and the FastAPI app as a
Docker service on **Render**.

**Production is English-only.** The image bakes only `bge-base-en-v1.5` and sets
`EMBED_MULTILINGUAL=false`. Non-English queries still work — the language is
still detected and reported — but they are embedded with the English model and
get no language boost, so pl/de/es quality is lower than it is locally.
Multilingual routing via bge-m3 remains available for local development, where
the flag defaults to true and the model downloads on first use.

---

## 0. Before you start

- The image builds for the host architecture. Render runs **amd64**; a Mac builds
  **arm64**. Render builds the Dockerfile itself, so this only matters if you
  build locally and push a registry image:
  `docker build --platform linux/amd64 -t songs-sense .`
- Image size: **3.53 GB compressed** (what Render pulls), 6.23 GB uncompressed on
  disk. Python site-packages is 5.3 GB of that — torch dominates — against
  419 MB of model weights. Note `docker images` reports *disk usage* including
  build cache, which reads much higher; use `docker images --tree` for the real
  content size.
- Do not set `EMBED_MULTILINGUAL=true` on this image. bge-m3 is not baked in, so
  the app would try to download ~2.2 GB from HuggingFace on first boot. If you
  ever want multilingual hosted, add the model back to the prefetch step in the
  `Dockerfile` and rebuild.

---

## 1. Neon

1. Create a Neon project. Note the connection string; it looks like
   `postgresql://user:pass@ep-xxx.region.aws.neon.tech/neondb?sslmode=require`.
   Export it locally as `NEON_URL` for the commands below.
2. Enable pgvector:
   ```bash
   psql "$NEON_URL" -c "CREATE EXTENSION IF NOT EXISTS vector;"
   ```
3. Export the local database:
   ```bash
   ./scripts/export_for_neon.sh
   ```
   Writes `data/neon/01_extension.sql` and `data/neon/02_songs_passages.dump`,
   and prints the size against Neon's free-tier limit. The dump carries both
   embedding columns — it doubles as a full local backup — and the multilingual
   one is dropped after restore in step 4c.

### The size problem

At the current corpus (9,381 songs / 85,879 passages) the compressed dump is
**730 MB**, against a **512 MB** free tier. It does not fit as-is. Restored, it
is larger again:

| Object | Size |
|---|---|
| `passages` table + indexes | 1,867 MB |
| — of which `embedding` (768d) | 252 MB |
| — of which `embedding_multi` (1024d) | 336 MB |
| — of which HNSW index on `embedding` | 325 MB |
| — of which HNSW index on `embedding_multi` | 651 MB |
| `songs` table | 21 MB |

`embedding_multi` and its HNSW index are **987 MB** of that, over half the
restored total, and English-only production never reads them. Dropping them
after restore is the standard step, not a fallback.

If that still does not fit, restore a subset of songs (filter by `tier`) for the
hosted demo, or move to a paid Neon plan.

4. Restore:
   ```bash
   # a. extension first
   psql "$NEON_URL" -f data/neon/01_extension.sql

   # b. schema + data (expect this to take a while; vectors are bulky)
   pg_restore --no-owner --no-privileges --verbose -d "$NEON_URL" \
     data/neon/02_songs_passages.dump

   # c. drop what English-only production does not use — reclaims ~987 MB
   psql "$NEON_URL" -c "DROP INDEX IF EXISTS passages_embedding_multi_hnsw_idx;"
   psql "$NEON_URL" -c "ALTER TABLE passages DROP COLUMN IF EXISTS embedding_multi;"
   ```

5. Confirm the HNSW index on `embedding` survived the restore; `pg_restore`
   normally carries it, but rebuild if not:
   ```bash
   psql "$NEON_URL" -c "\di+ passages*"
   # if passages_embedding_hnsw_idx is missing:
   psql "$NEON_URL" -c "CREATE INDEX passages_embedding_hnsw_idx
     ON passages USING hnsw (embedding vector_cosine_ops);"
   ```

6. Verify before wiring the app:
   ```bash
   psql "$NEON_URL" -c "SELECT count(*) FROM songs;"       # expect 9381
   psql "$NEON_URL" -c "SELECT count(*) FROM passages;"    # expect 85879
   psql "$NEON_URL" -c "SELECT count(*) FROM passages WHERE embedding IS NOT NULL;"
   ```

---

## 2. Render

1. New **Web Service** → connect the repo → runtime **Docker** (it will find the
   `Dockerfile` at the repo root).
2. Start command — Render injects `$PORT` and the image's default `CMD` already
   honours it, so leave it blank. If you must set it explicitly:
   ```
   uvicorn src.api.app:app --host 0.0.0.0 --port $PORT
   ```
3. Environment variables:

   | Key | Value |
   |---|---|
   | `DATABASE_URL` | the Neon connection string, keep `?sslmode=require` |

   `EMBED_MULTILINGUAL` is already `false` in the image and does not need setting.
   Nothing else is read at runtime — `GROQ_KEY` is eval-only.

### Instance size

Measured on the built image against the real corpus, cgroup high-water mark after
a batch of mixed-language queries:

| Models loaded | Peak RSS | Steady | Instance to pick |
|---|---|---|---|
| bge-base only | **0.75 GB** | 0.57 GB | **1–2 GB** |

1 GB fits with room to spare; 2 GB is the comfortable choice and leaves headroom
for concurrent requests. For reference, loading bge-m3 as well peaked at 1.95 GB
locally, which is why hosting it would mean a 4 GB instance — the cost that
motivated the English-only decision.

Figures are from an arm64 Docker Desktop build; amd64 will be in the same range
but re-check after the first deploy rather than assuming.

4. After the first boot, check the logs for the three startup lines:
   ```
   loaded BAAI/bge-base-en-v1.5 in ...s
   EMBED_MULTILINGUAL=false — skipping BAAI/bge-m3, English-only routing
   database ready in ...s (85879 passages)
   ```
   A missing "database ready" line means `DATABASE_URL` is wrong — the app logs
   the error and still starts, so it will fail at first query rather than at boot.

5. Smoke test:
   ```bash
   curl -s https://<service>.onrender.com/health
   # expect models: ["BAAI/bge-base-en-v1.5"]

   curl -s -X POST https://<service>.onrender.com/search/vibe \
     -H 'Content-Type: application/json' \
     -d '{"query":"late night drive, windows down, a little sad"}'
   ```

---

## 3. Notes

- The app opens a new Postgres connection per request. Fine locally (~0.1 s per
  query) but Neon adds round-trip latency and has connection limits; if the
  hosted instance feels slow or hits limits, a pooled connection is the first
  thing to try. Neon also offers a pooled connection string.
- No autoscaling, no worker processes: a single uvicorn process serves requests
  from a threadpool. Model inference is CPU-bound, so concurrency is limited by
  cores, not by the event loop.
- Free-tier Neon suspends idle databases; the first query after a suspension pays
  a wake-up delay. A paid Render instance does not sleep, so the model load cost
  is paid once at deploy.
- Local development keeps full multilingual routing: with `EMBED_MULTILINGUAL`
  unset the app defaults to true, loads bge-m3, and routes pl/de/es queries to it
  with the +0.1 language boost. The eval numbers in the README were produced that
  way, so hosted quality on non-English queries is below what the eval reports.
