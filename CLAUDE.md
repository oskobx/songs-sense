# songs-sense

A music discovery web app with three search modes over a curated corpus of ~25k popular songs. Built as a portfolio project to demonstrate real RAG, retrieval evaluation, and embedding fine-tuning skills.

## The three modes

1. **Vibe Search** — User describes a vibe or pastes a lyric → ranked songs with highlighted passages and LLM-generated explanations.
2. **Find the Song** — User describes a half-remembered song (fuzzy lyrics, vibe, era, context) → top candidates with iterative refinement.
3. **Lyric Twin** — User pastes any text → finds the closest matching real lyric passages. Screenshot-optimized.

All three share one backend, one corpus, one embedding index. Differences are in query handling and presentation.

## Tech stack

- **Language:** Python 3.12
- **Backend:** FastAPI (async)
- **Database:** PostgreSQL with pgvector extension (running locally via Docker for development)
- **Embeddings:** sentence-transformers, starting with `bge-base-en-v1.5`
- **Reranker:** `bge-reranker-base` (cross-encoder)
- **LLM:** Groq API (Llama models) for explanations and query understanding
- **Data sources:** Genius API (metadata), Spotify API (audio features/genre/year), LRClib + HuggingFace public datasets (lyrics), ScraperAPI (lyrics fallback)
- **Frontend:** Vanilla JavaScript, HTML, CSS (no React for now — keep it simple)
- **Deployment:** Render
- **Dev tools:** Ruff (lint + format), pytest (tests)

## Conventions

- Type hints on all function signatures
- Pydantic models for FastAPI inputs/outputs
- Async handlers in FastAPI; sync only where unavoidable
- Ruff for formatting (`ruff format`) and linting (`ruff check`)
- One module per concern; avoid god-files
- Tests in `tests/`, mirroring `src/` structure
- Environment variables loaded from `.env` via `python-dotenv`
- Never commit secrets — `.env` is gitignored, `.env.example` is committed

## Project structure (target)

```
songs-sense/
├── .venv/              # virtualenv (gitignored)
├── .env                # secrets (gitignored)
├── .env.example        # template with key names, no values
├── data/               # raw and processed data (gitignored)
├── notebooks/          # exploration, prototyping
├── src/
│   ├── corpus/         # scraping, ingestion, cleaning
│   ├── embeddings/     # embedding pipeline, fine-tuning
│   ├── retrieval/      # search, reranking, query routing
│   ├── api/            # FastAPI routes
│   ├── eval/           # eval harness, metrics, test sets
│   └── utils/
├── tests/              # mirrors src/ structure
├── frontend/           # JS/HTML/CSS
├── scripts/            # one-off scripts (run_scrape.py etc.)
├── docker-compose.yml  # local Postgres+pgvector
├── pyproject.toml
└── README.md

## Current focus

**Week 1: Project setup and corpus seed list.**
- Setting up dev environment (Python, Postgres via Docker, API keys)
- Building seed list of ~25k curated popular songs (Billboard + canonical + top artists)
- Evaluating coverage from free sources (LRClib, HuggingFace datasets) before resorting to ScraperAPI

## Don't do

- Don't suggest React/Next.js rewrites — vanilla JS frontend is intentional
- Don't suggest switching vector DBs (pgvector is chosen)
- Don't add new dependencies without flagging them and explaining why
- Don't write code that fetches lyrics outside of `src/corpus/`
- Don't commit anything in `data/` or `.env`
- Don't generate code that bypasses the eval harness once it exists — every retrieval change should be measurable
