# syntax=docker/dockerfile:1
#
# songs-sense API image.
#
# Both embedding models are baked in at build time: a cold container must not
# pull ~2.5 GB from HuggingFace on boot. That makes the image large but the
# boot fast and offline-safe. EMBED_MULTILINGUAL is read at runtime, so the
# same image serves both the full multilingual deployment and the cheaper
# English-only one — the m3 weights are simply not loaded when it is false.
#
# Build for the deploy target's architecture, not the laptop's:
#   docker build --platform linux/amd64 -t songs-sense .

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/models \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false

COPY --from=ghcr.io/astral-sh/uv:0.9.10 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so edits to src/ do not invalidate the heavy layers.
# zlib-state (pulled in by FlagEmbedding) ships no aarch64 wheel and compiles
# from source, so a toolchain is needed at install time only — installed and
# purged inside one layer so it never reaches the final image.
COPY pyproject.toml uv.lock ./
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential zlib1g-dev \
 && uv export --frozen --no-dev --no-emit-project --format requirements-txt \
      -o /tmp/requirements.txt \
 && uv pip install --system --no-cache -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt \
 && apt-get purge -y --auto-remove build-essential zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

# Bake the weights. Instantiating each model is what populates HF_HOME, and it
# also fails the build now rather than at 3am if a repo id ever moves.
RUN <<'PY' python
from FlagEmbedding import BGEM3FlagModel
from sentence_transformers import SentenceTransformer

SentenceTransformer("BAAI/bge-base-en-v1.5")
BGEM3FlagModel("BAAI/bge-m3", use_fp16=False)
print("models cached under /opt/models")
PY

COPY src/ ./src/
COPY static/ ./static/

# Default on; set to false on a small instance to skip the bge-m3 load.
ENV EMBED_MULTILINGUAL=true
EXPOSE 8000

# Render supplies $PORT; default to 8000 for local runs.
CMD ["sh", "-c", "uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
