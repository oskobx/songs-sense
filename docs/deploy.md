# Deployment — Neon + Render

Manual steps, in order. Nothing here is automated; run it once and note anything
that drifts.

Two moving parts: Postgres with pgvector on **Neon**, and the FastAPI app as a
Docker service on **Render**. The app holds the embedding models in memory; the
vector index lives on the database side, so app memory is essentially "which
models did you load".

---

## 0. Before you start

- The image builds for the host architecture. Render runs **amd64**; a Mac
  builds **arm64**. Render builds the Dockerfile itself, so this only matters if
  you build locally and push a registry image:
  `docker build --platform linux/amd64 -t songs-sense .`
- The image bakes both `bge-base-en-v1.5` and `bge-m3` and lands at **~6.25 GB**.
  If Render's build ever chokes on that, drop the `BGEM3FlagModel(...)` line from
  the prefetch step in the `Dockerfile` — with `EMBED_MULTILINGUAL=false` the m3
  weights are never loaded anyway, and the image drops by roughly 2.5 GB.

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
   and prints the size against Neon's free-tier limit.

### The size problem

As of the current corpus (9,381 songs / 85,879 passages) the compressed dump is
**730 MB**, against a **512 MB** free tier. It does not fit. Restored, it is
larger again:

| Object | Size |
|---|---|
| `passages` table + indexes | 1,867 MB |
| — of which `embedding` (768d) | 252 MB |
| — of which `embedding_multi` (1024d) | 336 MB |
| — of which HNSW index on `embedding` | 325 MB |
| — of which HNSW index on `embedding_multi` | 651 MB |
| `songs` table | 21 MB |

The multilingual column and its HNSW index are **987 MB of that**, over half the
total, and production runs `EMBED_MULTILINGUAL=false`. So the cheap fix is to
restore everything and then drop what production will not use (step 4c below).

Alternatives if you want multilingual search hosted: pay for a larger Neon plan,
or restore a subset of songs (filter by `tier`) for the demo.

4. Restore:
   ```bash
   # a. extension first
   psql "$NEON_URL" -f data/neon/01_extension.sql

   # b. schema + data (expect this to take a while; vectors are bulky)
   pg_restore --no-owner --no-privileges --verbose -d "$NEON_URL" \
     data/neon/02_songs_passages.dump

   # c. only if you are staying on a small plan — reclaims ~987 MB
   psql "$NEON_URL" -c "DROP INDEX IF EXISTS passages_embedding_multi_hnsw_idx;"
   psql "$NEON_URL" -c "ALTER TABLE passages DROP COLUMN IF EXISTS embedding_multi;"
   ```
   Dropping `embedding_multi` makes `EMBED_MULTILINGUAL=true` impossible on that
   database — `semantic_search` selects that column for pl/de/es queries and will
   error. Keep the two settings consistent.

5. Confirm the HNSW index survived the restore; `pg_restore` normally carries it,
   but rebuild if not:
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
2. Start command — Render injects `$PORT`, and the image's default `CMD` already
   honours it, so leave it blank. If you must set it explicitly:
   ```
   uvicorn src.api.app:app --host 0.0.0.0 --port $PORT
   ```
3. Environment variables:

   | Key | Value |
   |---|---|
   | `DATABASE_URL` | the Neon connection string, keep `?sslmode=require` |
   | `EMBED_MULTILINGUAL` | `false` for the small instance, `true` for the large one |

   Nothing else is read at runtime. `GROQ_KEY` is eval-only and not needed here.

### Instance size

Measured on the built image with the real corpus, cgroup high-water mark after
twelve mixed-language queries:

| Setting | Models loaded | Peak RSS | Steady | Instance to pick |
|---|---|---|---|---|
| `EMBED_MULTILINGUAL=true` | bge-base + bge-m3 | **1.95 GB** | 1.81 GB | **4 GB** |
| `EMBED_MULTILINGUAL=false` | bge-base only | **0.56 GB** | 0.57 GB | **1–2 GB** |

Do not put the multilingual setting on a 2 GB instance: 1.95 GB peak leaves no
room for the container runtime, and the OOM killer arrives mid-request. 4 GB is
the safe tier. English-only fits 1 GB but 2 GB is the comfortable choice and
leaves headroom for concurrency.

These figures are from an arm64 Docker Desktop build; amd64 will be in the same
range but re-check after the first deploy rather than assuming.

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
   curl -s -X POST https://<service>.onrender.com/search/vibe \
     -H 'Content-Type: application/json' \
     -d '{"query":"late night drive, windows down, a little sad"}'
   ```
   `/health` should list exactly the models you expect for the setting.

---

## 3. Switching between the two modes

**Down to English-only** (cheaper instance): set `EMBED_MULTILINGUAL=false`,
redeploy, resize the instance down. Optionally drop `embedding_multi` on Neon to
reclaim storage. Non-English queries still work — the language is still detected
and reported — but they are embedded with the English model and get no language
boost, so quality on pl/de/es drops.

**Up to multilingual**: the column and its index must exist on the database. If
you dropped them, re-restore `passages` from a fresh dump, rebuild the HNSW
index, then set `EMBED_MULTILINGUAL=true` and resize up to 4 GB.

---

## 4. Notes

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
