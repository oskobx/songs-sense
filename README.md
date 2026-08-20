# songs-sense

**Live demo — <https://songs-sense.onrender.com>**

A multi-mode music search platform: vibe search, fuzzy song retrieval, and lyric-to-text matching over a curated corpus of popular songs. Built as a portfolio project demonstrating RAG, retrieval evaluation, and embedding fine-tuning. Work in progress.

## Modes

**Vibe Search** is live: describe a feeling and get ranked songs with the passage
that matched. **Find the Song** (hybrid BM25 + semantic, for half-remembered
lyrics) and **Lyric Twin** (closest real lyric to any text) exist as retrieval
profiles in `src/retrieval/` and are reachable from `scripts/search.py --mode`,
but are not exposed in the web UI.

## Run locally

Requires Docker, Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
docker compose up -d                          # Postgres 16 + pgvector on :5432
uv sync
cp .env.example .env                          # fill in DATABASE_URL at minimum
uv run uvicorn src.api.app:app --reload       # http://localhost:8000
```

| Variable | Needed for |
|---|---|
| `DATABASE_URL` | always |
| `EMBED_MULTILINGUAL` | optional; defaults to `true` locally, loading bge-m3 for pl/de/es |
| `GROQ_KEY` | the eval harness only, not the app |

The corpus itself is not in the repo. Building it from scratch means running the
`src/corpus/` and `src/embeddings/` scripts against the Genius, Spotify and
LRClib sources, which takes hours and needs API keys.

## Phase 3b: Vibe Search evaluation

A reproducible benchmark for Vibe Search, so later changes (reranker, fine-tuned
embeddings, chunk tuning) can be measured rather than guessed at.

### How it works

114 curated vibe queries (81 en, 19 pl, 9 de, 5 es) each retrieve the top 10
passages. An LLM judge (`openai/gpt-oss-120b` via Groq) grades every
(query, passage) pair 0–3 on how well the passage delivers the feeling the query
describes. Judgments are cached on `(query_id, passage_id)`, namespaced by model
and prompt version, so re-running against unchanged retrieval is nearly free.

The judge is worth only as much as its agreement with a human, so 55 pairs were
hand-graded: sampled across three rank bands (1–3, 4–10, 15–30) so the set
contains low grades, and restricted to en/pl since the grader cannot reliably
judge de or es. The 55 split into two batches drawn from disjoint queries —
batch 1 (30 pairs) drove three rounds of judge-prompt iteration, batch 2 (25)
was held out and scored once. Batch 1 was then re-graded blind (same pairs,
fresh order, original grades hidden), and the judge re-scored pairs twice.

| Comparison | Statistic | Value | n |
|---|---|---|---|
| Judge vs human, held-out batch 2 | quadratic-weighted kappa | 0.46 | 25 |
| Judge vs human, tuning batch 1 (re-graded) | quadratic-weighted kappa | 0.34 | 30 |
| Human vs self, blind re-grade | quadratic-weighted kappa | 0.42 | 30 |
| Judge vs self, repeated calls | exact-repeat rate | 0.88 | 50 |

The judge agrees with the grader about as closely as the grader agrees with
themselves, so the ceiling here is human labelling noise rather than the model.
Read the kappa as near the floor of what the task can resolve; further prompt
tuning against a target this noisy would be fitting to noise.

### Baseline

Judge prompt v3, semantic-only retrieval, language boost +0.1, no reranker,
8-line chunks, over ~9.4k songs and 86k passages.

| Slice | n | recall@1 | recall@5 | recall@10 | MRR | NDCG@10 |
|---|---|---|---|---|---|---|
| Overall | 114 | 0.50 | 0.82 | 0.89 | 0.63 | 0.80 |
| en | 81 | 0.48 | 0.84 | 0.90 | 0.63 | 0.81 |
| pl | 19 | 0.53 | 0.74 | 0.84 | 0.62 | 0.78 |
| de | 9 | 0.44 | 0.67 | 0.78 | 0.57 | 0.73 |
| es | 5 | 0.80 | 1.00 | 1.00 | 0.90 | 0.88 |

The eval found one concrete bug. The language detector was built with only
English, Polish and German, so every Spanish query was detected as `en` or `pl`,
routed to the English-only embedding model, and boosted toward the wrong
language. Adding Spanish to the detector and to the multilingual routing set
moved es from MRR 0.46 / NDCG 0.71 / recall@1 0.20 to 0.90 / 0.88 / 0.80, and
overall MRR from 0.61 to 0.63. Detection now matches the declared language on
all 114 queries.

### Limitations

- The judge sits at the label noise floor, so absolute numbers are provisional.
- Calibration covers en/pl only; the de and es rows rest on a judge never
  checked against a human in those languages.
- n = 9 (de) and n = 5 (es) are too small to read as language comparisons.
- NDCG@10 uses each query's observed grades as its ideal ranking, the usual
  practical approximation. It flatters queries whose best available result is
  mediocre, which is why NDCG 0.80 sits alongside recall@1 0.50.
- An earlier judge version cached a 0 for unparseable responses, indistinguishable
  from a genuine "not relevant". All 233 affected entries were re-judged; 93
  changed against a 26% baseline churn from judge nondeterminism, so roughly 30
  were real repairs. Individual entries cannot be attributed.
- Covers Vibe Search only. Find the Song and Lyric Twin are unevaluated.
- Lyric excerpts in the committed eval files are truncated to two lines; the
  full passages exist only in the database.

### Running it

```bash
python -m src.eval.generate_vibe_queries            # LLM-generated candidates
python -m src.eval.curate_queries                   # keep/delete CLI
python -m src.eval.grade_calibration                # sample + grade 30 pairs
python -m src.eval.grade_calibration --add 25       # held-out batch
python -m src.eval.grade_calibration --regrade 1    # blind re-grade
python -m src.eval.calibrate --versions v3 --self-consistency 10
python -m src.eval.run_vibe_eval --note "what changed"
```

Results land in `data/eval/results_<timestamp>.json` with per-query detail and
the retrieval config. Needs `GROQ_KEY` in `.env` and a populated local Postgres.

## Deployment

Postgres on **Neon** (Launch plan, compute capped at 0.5 CU, `us-west-2`), app on
**Render** as a Docker service. Full runbook, including the export and restore
procedure, in [docs/deploy.md](docs/deploy.md).

Production runs **English-only**. The image bakes only `bge-base-en-v1.5` and sets
`EMBED_MULTILINGUAL=false`, which holds peak memory at 0.75 GB instead of 1.95 GB
and roughly halves the hosted database. Non-English queries still work — the
language is detected and reported — but they are embedded with the English model
and get no language boost, so pl/de/es quality is below what the eval above
reports. Multilingual routing via bge-m3 stays available locally, where the flag
defaults to true.

The hosted database is 714 MB: 9,381 songs and 85,879 passages with 768-d
embeddings and an HNSW index, minus the bge-m3 vectors and the full-text column
that only the unexposed BM25 profile uses.

Measured against the live instance:

| Request | Warm |
|---|---|
| `POST /search/vibe` | 0.90–1.61 s (median ~1.1 s, n=8 distinct queries) |
| `GET /` | 0.17 s |
| `GET /health` | 0.16 s |

A paid Render instance does not sleep, so there is no cold start in normal
operation — the ~1 s model load is paid once per deploy.

Almost none of the ~1 s is the vector search, which takes about 1 ms
server-side, and about 0.16 s is network to Render. The rest is query embedding
on the instance plus app-to-database round trips. The app holds one database
connection for the life of the process rather than opening one per request,
which assumes a single worker; that removed a connection setup per request but
did not measurably change end-to-end latency, so the remaining time is compute,
not connection overhead.

`GET /health` reports the running commit, so which build is live is a question
you can answer with one request.

Cost is about $25/month for the Render instance, plus a few dollars a month of
Neon and Groq usage.
