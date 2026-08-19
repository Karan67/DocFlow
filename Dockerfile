# Multi-target build.
#
#   base    shared runtime dependencies
#   api     + application code            -> API, beat, flower
#   worker  + OCR system packages, processing deps, embedding model -> worker pools
#   dev     + test dependencies           -> local development and CI
#
# The split exists because the API needs none of the heavy half: no tesseract,
# no poppler, no 130MB embedding model. Building them as one image made every
# deployment ship ~950MB to a service that parses no documents at all.
#
# Celery 5.x does not yet officially support Python 3.14; pinning 3.12 here
# makes the local interpreter version irrelevant.

# --- base ---------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# curl is used by the compose healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 appuser

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# --- api ----------------------------------------------------------------
FROM base AS api

COPY . .

# Create the upload directory, owned by appuser, BEFORE the named volume is
# first mounted: Docker seeds an empty named volume from the image directory,
# ownership included. Skip this and the non-root process cannot write to it.
RUN mkdir -p /data/uploads \
 && chown -R appuser:appuser /data/uploads /app

USER appuser
EXPOSE 8000
CMD ["sh", "/app/scripts/start-api.sh"]

# --- worker -------------------------------------------------------------
FROM base AS worker

ENV FASTEMBED_CACHE_PATH=/opt/fastembed

# tesseract   - OCR engine; -eng is a separate package and is NOT pulled in by
#               --no-install-recommends, so it must be named explicitly
# poppler     - pdftoppm, which pdf2image shells out to for page rendering
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr \
      tesseract-ocr-eng \
      poppler-utils \
 && rm -rf /var/lib/apt/lists/*

# fastembed and the model get their own layers ahead of the remaining
# requirements, so adding a dependency rebuilds pip without re-downloading the
# ~130MB model. Baking it in at all keeps a network fetch out of the hot path.
RUN pip install --no-cache-dir "fastembed>=0.4.0"
RUN mkdir -p "$FASTEMBED_CACHE_PATH" \
 && python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='$FASTEMBED_CACHE_PATH')" \
 && chown -R appuser:appuser "$FASTEMBED_CACHE_PATH"

COPY requirements-worker.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements-worker.txt

COPY . .

RUN mkdir -p /data/uploads \
 && chown -R appuser:appuser /data/uploads /app

USER appuser
CMD ["celery", "-A", "worker.celery_app", "worker", "--loglevel=INFO"]

# --- dev ----------------------------------------------------------------
# The worker image plus test dependencies. Tests drive the full pipeline, so
# they need the processing half; this is what local compose and CI run.
FROM worker AS dev

USER root
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt \
 && chown -R appuser:appuser /app
USER appuser
