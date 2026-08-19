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
the flag defaults to true.

---

## 0. Postgres client tools

**There is no `psql`, `pg_dump` or `pg_restore` on this machine.** Every command
below therefore borrows them from the running Postgres container, which has all
three at the same version that produced the dump:

```bash
docker exec -i songs-sense-db psql "$NEON_URL" -c "SELECT 1;"
```

`docker exec -i` (not `-it`) is what lets you pipe a dump file in on stdin. The
container has working DNS and can reach Neon directly.

If you would rather install them natively: `brew install libpq` and add
`/opt/homebrew/opt/libpq/bin` to your `PATH`. Nothing here requires it.

Put the Neon connection string in `.env` as `NEON_URL=...` (the file is
gitignored) and load it with `set -a; . ./.env; set +a` before running these.

Other prerequisites: the code must be pushed to GitHub — Render builds from the
repo, not from your working tree.

---

## 1. Export

```bash
./scripts/export_for_neon.sh
```

Writes `data/neon/01_extension.sql` and `data/neon/02_songs_passages_nomulti.dump`.

The default dump **excludes `embedding_multi` and its HNSW index**. This is the
normal path, not a fallback: production is English-only, and that column plus its
index is more than half the restored size. Stripping before upload beats
restoring everything and dropping it afterwards, which risks running out of
storage mid-restore and wastes a long upload.

The strip happens on a throwaway `TEMPLATE` copy of the local database, so your
local data is untouched and every other index, constraint and trigger survives.
Use `--with-multi` when you want a complete backup instead.

### Will it fit?

Measured by restoring the stripped dump into a fresh local database — the same
thing Neon will hold:

| | Size |
|---|---|
| Stripped dump file (compressed) | 325 MB |
| **Restored database** | **751 MB** |
| — `passages` table + indexes | 729 MB |
| — of which HNSW index on `embedding` | 323 MB |
| — `songs` table | 14 MB |

For comparison, the unstripped dump is 730 MB as a file and would restore to
roughly 1.7 GB.

**Check the 751 MB against your Neon plan's storage allowance before uploading.**
If it does not fit, in order of cost:

1. Drop the full-text column too — `passage_tsv` and its GIN index are ~35 MB and
   only serve BM25, which the API does not expose:
   ```sql
   DROP INDEX passages_passage_tsv_idx;
   ALTER TABLE passages DROP COLUMN passage_tsv;
   ```
2. Restore a subset of songs. Roughly half the corpus lands near 390 MB. Filter
   by `tier` so the demo keeps the recognisable songs.
3. Move to a paid plan.

---

## 2. Neon

1. Create a Neon project. Note the connection string — it looks like
   `postgresql://user:pass@ep-xxx.region.aws.neon.tech/neondb?sslmode=require`.
   Pick the region closest to where Render will run.
2. Enable pgvector:
   ```bash
   docker exec -i songs-sense-db psql "$NEON_URL" -f - < data/neon/01_extension.sql
   ```
3. Restore. This uploads ~325 MB and then rebuilds the HNSW index server-side, so
   expect it to take a while:
   ```bash
   docker exec -i songs-sense-db pg_restore --no-owner --no-privileges \
     -d "$NEON_URL" < data/neon/02_songs_passages_nomulti.dump
   ```
   Pass `-U postgres` only when restoring into a *local* database; the Neon URL
   already carries its own user.
4. Verify:
   ```bash
   docker exec -i songs-sense-db psql "$NEON_URL" -c "
     SELECT (SELECT count(*) FROM songs)    AS songs,
            (SELECT count(*) FROM passages) AS passages,
            pg_size_pretty(pg_database_size(current_database())) AS size;"
   ```
   Expect 9381 songs and 85879 passages.
5. Confirm the HNSW index came across — `pg_restore` normally carries it:
   ```bash
   docker exec -i songs-sense-db psql "$NEON_URL" -c "\di+ passages*"
   # if passages_embedding_hnsw_idx is missing:
   docker exec -i songs-sense-db psql "$NEON_URL" -c "
     CREATE INDEX passages_embedding_hnsw_idx
       ON passages USING hnsw (embedding vector_cosine_ops)
       WITH (m = 16, ef_construction = 64);"
   ```
6. Smoke-test a vector query before wiring the app:
   ```bash
   docker exec -i songs-sense-db psql "$NEON_URL" -tAc "
     SELECT s.artist || ' — ' || s.title
     FROM passages p JOIN songs s ON p.song_id = s.id
     ORDER BY p.embedding <=> (SELECT embedding FROM passages LIMIT 1) LIMIT 3;"
   ```

---

## 3. Render

1. New **Web Service** → connect the repo → runtime **Docker** (it finds the
   `Dockerfile` at the repo root).
2. Start command — Render injects `$PORT` and the image's `CMD` already honours
   it, so leave it blank. Explicitly, it would be:
   ```
   uvicorn src.api.app:app --host 0.0.0.0 --port $PORT
   ```
3. Environment variables:

   | Key | Value |
   |---|---|
   | `DATABASE_URL` | the Neon connection string, keep `?sslmode=require` |

   `EMBED_MULTILINGUAL` is already `false` in the image. Nothing else is read at
   runtime — `GROQ_KEY` is eval-only.

### Instance size

Measured on the built image against the real corpus, cgroup high-water mark after
a batch of mixed-language queries:

| Models loaded | Peak RSS | Steady | Instance to pick |
|---|---|---|---|
| bge-base only | **0.75 GB** | 0.57 GB | **1–2 GB** |

1 GB fits with room to spare; 2 GB is comfortable and leaves headroom for
concurrent requests. Loading bge-m3 as well peaked at 1.95 GB locally, which
would mean a 4 GB instance — the cost that motivated English-only.

Image size is 3.53 GB compressed, 6.23 GB uncompressed, of which Python
site-packages is 5.3 GB (torch dominates) against 419 MB of model weights. Note
`docker images` reports *disk usage including build cache*, which reads much
higher; `docker images --tree` shows the real content size.

Build for the deploy target's architecture if you ever push a prebuilt image —
Render is amd64, a Mac is arm64:
`docker build --platform linux/amd64 -t songs-sense .`

Do not set `EMBED_MULTILINGUAL=true` on this image: bge-m3 is not baked in, so
the app would try to download ~2.2 GB on first boot. Add it back to the prefetch
step in the `Dockerfile` and rebuild if you ever want multilingual hosted.

4. After the first boot, check the logs for three lines:
   ```
   loaded BAAI/bge-base-en-v1.5 in ...s
   EMBED_MULTILINGUAL=false — skipping BAAI/bge-m3, English-only routing
   database ready in ...s (85879 passages)
   ```
   A missing "database ready" line means `DATABASE_URL` is wrong. The app logs
   the error and starts anyway, so it fails at first query rather than at boot.

5. Smoke test:
   ```bash
   curl -s https://<service>.onrender.com/health
   # expect models: ["BAAI/bge-base-en-v1.5"]

   curl -s -X POST https://<service>.onrender.com/search/vibe \
     -H 'Content-Type: application/json' \
     -d '{"query":"late night drive, windows down, a little sad"}'
   ```

---

## 4. Notes

- The app opens a new Postgres connection per request. Fine locally (~0.1 s per
  query), but Neon adds round-trip latency and has connection limits. If the
  hosted instance feels slow, switch `DATABASE_URL` to Neon's pooled connection
  string first.
- No autoscaling, no worker processes: one uvicorn process serving from a
  threadpool. Model inference is CPU-bound, so concurrency is limited by cores.
- Free-tier Neon suspends idle databases; the first query after a suspension pays
  a wake-up delay. A paid Render instance does not sleep, so the model load is
  paid once at deploy.
- Local development keeps full multilingual routing: with `EMBED_MULTILINGUAL`
  unset the app defaults to true, loads bge-m3, and routes pl/de/es queries to it
  with the +0.1 boost. The README's eval numbers were produced that way, so
  hosted quality on non-English queries is below what the eval reports.
