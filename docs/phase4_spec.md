# Phase 4 — Minimal API, UI, and Deployment (Vibe Search only)

Goal: make songs-sense demoable with a live URL. Smallest possible surface. No new retrieval logic, no new modes, no reranker. Find the Song and Lyric Twin stay as implemented retrieval profiles without UI.

Timebox: ~3–4 working sessions. If something here turns into a rabbit hole, cut it and note it in the README.

---

## 1. Backend — FastAPI

New module `src/api/app.py`.

**Endpoint**

```
POST /search/vibe
body:    {"query": str, "k": int = 10}
returns: {
  "query": str,
  "detected_language": str,
  "results": [
    {"rank": int, "artist": str, "title": str, "year": int | null,
     "passage": str, "score": float}
  ]
}
```

- Calls the existing vibe retrieval function (semantic-only profile, language routing, +0.1 boost). No duplication of retrieval logic — import and call.
- `k` clamped to 1–25. Empty query → 400.
- Aggregate passages to songs: one row per song, keep the best-scoring passage as `passage`.
- Return 10 by default.

**Health**

```
GET /health  → {"status": "ok", "models": [...loaded model names...]}
```

**Startup**

- Load embedding model(s) once at app startup (FastAPI lifespan), not per request.
- Env flag `EMBED_MULTILINGUAL` (default `true`). When `false`: do not load bge-m3; route every query to bge-base regardless of detected language; still return `detected_language`. This is the production setting to save ~1 GB RAM.
- `DATABASE_URL` from env (already the pattern for scripts — reuse).
- Log model load time and DB connect at startup.

**Serving the page**

- `GET /` serves `static/index.html`. Mount `static/` for any assets (keep it to one HTML file if possible).

**Run**

- `uv run uvicorn src.api.app:app --reload` documented in README.

**Tests** (light)

- One test that `POST /search/vibe` with a stubbed retrieval function returns the right shape and clamps `k`.
- One test that `EMBED_MULTILINGUAL=false` skips bge-m3 load (mock the loader).

---

## 2. Frontend — one static page

`static/index.html`, vanilla JS, no build step, no framework, no external CSS/JS except optionally a system font stack.

- Title "songs-sense — Vibe Search", one-line description: "Describe a feeling, get songs."
- Text input (placeholder: e.g. "late night drive, windows down, a little sad"), submit button, Enter submits.
- While loading: disable button, show "searching…".
- Results: ordered list, each item shows **Artist — Title** (year if present) and the matching passage lines below in a quieter style. Show the detected language as a small label above the list.
- Error state: one line of text if the request fails.
- Keep styling minimal and readable — mobile-friendly width, no animations. ~100–150 lines total.

---

## 3. Deployment

**Database → Neon (free tier, pgvector).**

- Create a Neon project, enable the `vector` extension.
- Dump the local DB and restore into Neon: `pg_dump` of the schema + `songs` + `passages` tables including `embedding` and (optionally) `embedding_multi` and indexes. If `embedding_multi` + its HNSW index pushes past Neon's free storage, drop it for the hosted copy — prod runs `EMBED_MULTILINGUAL=false` anyway.
- Rebuild HNSW index after restore if `pg_dump` didn't carry it.
- Verify with one query via `psql` before wiring the app.

**App → Render (paid instance, ~2 GB RAM).**

- Second Render web service, Docker deploy from the repo.
- `Dockerfile`: python 3.12 slim, install deps via `uv`, copy `src/` and `static/`, pre-download bge-base at build time so it's baked into the image (avoids a cold download on every boot). Do NOT bake bge-m3.
- Env on Render: `DATABASE_URL` (Neon), `EMBED_MULTILINGUAL=false`, anything else `src/` already reads.
- Start command: `uvicorn src.api.app:app --host 0.0.0.0 --port $PORT`.
- Check memory after first boot; if it's near the ceiling, note it.

**Not needed:** UptimeRobot (paid instance doesn't sleep), a separate frontend host (FastAPI serves the page).

---

## 4. README additions

- "Live demo" link at the top.
- "Run locally" section: Docker Postgres, `uv sync`, env vars, `uv run uvicorn …`.
- "Deployment" section: Neon + Render, the `EMBED_MULTILINGUAL=false` tradeoff (English-only embeddings in prod; multilingual routing available locally), rough cost.
- Short "Modes" note: Vibe Search live; Find the Song / Lyric Twin implemented as retrieval profiles, not exposed.

---

## 5. Acceptance

- `curl -X POST localhost:8000/search/vibe -d '{"query":"…"}'` returns 10 songs with passages.
- The page works end-to-end locally.
- Same on the Render URL, first-load response under ~3 s for a warm instance.
- `git log` shows small commits per step (api, page, Dockerfile, deploy notes), not one blob.

## 6. Out of scope (do not do)

- Any change to retrieval, chunking, embeddings, or eval.
- Auth, rate limiting, caching layers, analytics.
- Find the Song / Lyric Twin UI.
- A JS framework or build tooling.