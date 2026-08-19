# syntax=docker/dockerfile:1
#
# songs-sense API image.
#
# Production is English-only, so only bge-base is baked in. A cold container
# must not pull weights from HuggingFace on boot, and baking bge-m3 as well
# would add ~2.5 GB that this deployment never loads.
#
# EMBED_MULTILINGUAL is still read at runtime and still defaults to true in the
# application, so local runs outside this image keep multilingual routing. The
# image sets it to false. Setting it back to true *in this image* would make the
# app download bge-m3 on first boot — don't; rebuild with the model baked in.
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

# Bake the weights. Instantiating the model is what populates HF_HOME, and it
# also fails the build now rather than at 3am if a repo id ever moves.
RUN <<'PY' python
from sentence_transformers import SentenceTransformer

SentenceTransformer("BAAI/bge-base-en-v1.5")
print("bge-base cached under /opt/models")
PY

COPY src/ ./src/
COPY static/ ./static/

# English-only: bge-m3 is not in this image, so it must not be loaded.
ENV EMBED_MULTILINGUAL=false
EXPOSE 8000

# Render supplies $PORT; default to 8000 for local runs.
CMD ["sh", "-c", "uvicorn src.api.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
