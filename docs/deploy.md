# Deployment — Neon + Render

Manual steps, in order. The Neon half has been run; timings below are measured,
not estimated. The Render half has not been run yet.

Two moving parts: Postgres with pgvector on **Neon**, and the FastAPI app as a
Docker service on **Render**.

**Production is English-only.** The image bakes only `bge-base-en-v1.5` and sets
`EMBED_MULTILINGUAL=false`. Non-English queries still work — the language is
still detected and reported — but they are embedded with the English model and
get no language boost, so pl/de/es quality is lower than it is locally.
Multilingual routing via bge-m3 remains available for local development, where
the flag defaults to true.

---

## 0. Client tools and the connection string

**There is no `psql`, `pg_dump` or `pg_restore` on this machine.** Every command
below borrows them from the running Postgres container, which has all three at
the same version that produced the dump:

```bash
docker exec -i songs-sense-db psql "$NEON_URL" -c "SELECT 1;"
```

Use `docker exec -i`, not `-it` — the `-i` is what lets you pipe a dump in on
stdin, and `-t` breaks piping. The container has working DNS and reaches Neon
directly. To install natively instead: `brew install libpq`, then add
`/opt/homebrew/opt/libpq/bin` to `PATH`. Nothing here requires it.

Put the connection string in `.env` as `NEON_URL=...` (gitignored). **Do not
`source .env`**: Neon's URL ends in `?sslmode=require&channel_binding=require`,
and the unquoted `&` is a syntax error in zsh. Read just that one value:

```bash
NEON_URL="$(grep -E '^NEON_URL=' .env | head -1 | cut -d= -f2-)"
```

Use the **direct** connection string for restores. Neon's pooled endpoint is for
the app's many short connections, not for one long `pg_restore`.

Other prerequisites: push to GitHub — Render builds from the repo, not your
working tree.

---

## 1. Export

```bash
./scripts/export_for_neon.sh          # ~2 min, writes data/neon/
```

Produces `01_extension.sql` and `02_songs_passages_lean.dump` (**314.8 MB**).

The default dump strips two things the hosted app never reads:

| Dropped | Why |
|---|---|
| `embedding_multi` + its HNSW index | bge-m3 vectors; production is English-only. Over half the restored size. |
| `passage_tsv` + its GIN index + trigger | Full-text column for BM25, used only by the unexposed "Find the Song" profile. |

Stripping happens *before* upload, on a throwaway `TEMPLATE` clone of the local
database — your local data is untouched, and every other index, constraint and
trigger survives. Doing it this way rather than dropping columns after the
restore matters: `DROP COLUMN` does not reclaim space without a `VACUUM FULL`,
which is slow on a small instance and can run you out of storage mid-restore.

`--with-multi` and `--with-fts` each put one back; pass both for a full backup.

For reference: full dump 730 MB (restores to ~1.7 GB), multi-stripped only
325 MB, lean 314.8 MB.

---

## 2. Neon — measured run

Plan: **Launch**, compute capped at 0.5 CU, region `us-west-2`, Postgres 16.15,
pgvector 0.8.0.

```bash
NEON_URL="$(grep -E '^NEON_URL=' .env | head -1 | cut -d= -f2-)"

# 1. extension — 2s
docker exec -i songs-sense-db psql "$NEON_URL" -v ON_ERROR_STOP=1 \
  -f - < data/neon/01_extension.sql

# 2. restore — 396s (data loaded by ~100s, the rest is the HNSW build)
docker exec -i songs-sense-db pg_restore --no-owner --no-privileges --verbose \
  -d "$NEON_URL" < data/neon/02_songs_passages_lean.dump

# 3. verify
docker exec -i songs-sense-db psql "$NEON_URL" -c "
  SELECT (SELECT count(*) FROM songs)                                AS songs,
         (SELECT count(*) FROM passages)                             AS passages,
         (SELECT count(*) FROM passages WHERE embedding IS NOT NULL) AS with_embedding,
         pg_size_pretty(pg_database_size(current_database()))        AS size;"

# 4. confirm the HNSW index arrived (it does; pg_restore carries it)
docker exec -i songs-sense-db psql "$NEON_URL" -c "\di+ passages*"
```

Pass `-U postgres` only when restoring into a *local* database — the Neon URL
carries its own user. Omitting it locally makes `pg_restore` connect as `root`
and fail.

Result:

| | |
|---|---|
| songs / passages | 9381 / 85879, all 85879 with embeddings |
| database size | **714 MB** |
| `passages` | 693 MB (HNSW on `embedding`: 323 MB) |
| `songs` | 14 MB |
| indexes present | `passages_embedding_hnsw_idx`, `passages_pkey`, `passages_song_id_idx`, `passages_language_idx`, FK to `songs` |

No index rebuild was needed. If `passages_embedding_hnsw_idx` were ever missing:

```sql
CREATE INDEX passages_embedding_hnsw_idx ON passages
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
```

### Latency — this decides the Render region

Measured from a laptop in Massachusetts against `us-west-2`:

| | |
|---|---|
| Round trip (`SELECT 1`) | 101 ms |
| ANN search over 85,879 rows | 102 ms |
| **Query work minus network** | **1 ms** |

The vector search costs about a millisecond. Everything else is round-trip
latency. Two consequences:

- **Create the Render service in Oregon (`us-west`)**, next to the database. From
  Massachusetts a search took ~620 ms end to end; colocated it should be tens of
  milliseconds. If Render must live on the East Coast, move the Neon project
  instead.
- The 0.5 CU compute cap is **not** a bottleneck for this workload. Don't pay to
  raise it.

The app also opens a new connection per request, so each search pays a fresh
handshake. Once app and database are colocated that matters less; if it still
shows, switch `DATABASE_URL` to Neon's pooled connection string.

---

## 3. Render — not yet run

1. New **Web Service** → connect the repo → runtime **Docker** (it finds the
   `Dockerfile` at the repo root). Region: **Oregon**, per the latency note above.
2. Start command — Render injects `$PORT` and the image's `CMD` honours it, so
   leave it blank. Explicitly it would be:
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
concurrency. Loading bge-m3 as well peaked at 1.95 GB locally, which would mean a
4 GB instance — the cost that motivated English-only.

Image is 3.53 GB compressed, 6.23 GB uncompressed, of which site-packages is
5.3 GB (torch dominates) against 419 MB of weights. `docker images` reports disk
usage including build cache and reads much higher; `docker images --tree` shows
real content size.

Build for the target architecture if you ever push a prebuilt image — Render is
amd64, a Mac is arm64: `docker build --platform linux/amd64 -t songs-sense .`

Do not set `EMBED_MULTILINGUAL=true` on this image: bge-m3 is not baked in, so
the app would try to download ~2.2 GB on first boot. Add it back to the prefetch
step in the `Dockerfile` and rebuild if you want multilingual hosted.

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

- No autoscaling, no worker processes: one uvicorn process serving from a
  threadpool. Model inference is CPU-bound, so concurrency is limited by cores.
- A paid Render instance does not sleep, so the model load is paid once at deploy.
- Local development keeps full multilingual routing: with `EMBED_MULTILINGUAL`
  unset the app defaults to true, loads bge-m3, and routes pl/de/es queries to it
  with the +0.1 boost. The README's eval numbers were produced that way, so hosted
  quality on non-English queries is below what the eval reports.
- The hosted database has no `passage_tsv`, so BM25 / "Find the Song" cannot run
  against it. Re-export with `--with-fts` if that mode is ever exposed.
