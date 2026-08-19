FROM python:3.12-slim

# Celery 5.x does not yet officially support Python 3.14; the image pins 3.12
# so the local interpreter version is irrelevant.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    FASTEMBED_CACHE_PATH=/opt/fastembed

WORKDIR /app

# curl        - compose healthcheck
# tesseract   - OCR engine; -eng is a separate package and is NOT pulled in by
#               --no-install-recommends, so it must be named explicitly
# poppler     - pdftoppm, which pdf2image shells out to for page rendering
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl \
      tesseract-ocr \
      tesseract-ocr-eng \
      poppler-utils \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 appuser

# fastembed and the model get their own early layers, ahead of the rest of the
# requirements. Adding or bumping a dependency then rebuilds pip only - it does
# not re-download the ~130MB model.
#
# Baking the model in at all is deliberate: fetching it lazily on the first
# task would put a network dependency in the hot path.
RUN pip install --no-cache-dir "fastembed>=0.4.0"
RUN mkdir -p "$FASTEMBED_CACHE_PATH" \
 && python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='$FASTEMBED_CACHE_PATH')" \
 && chown -R appuser:appuser "$FASTEMBED_CACHE_PATH"

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY . .

# Create the upload directory, owned by appuser, BEFORE the named volume is
# first mounted: Docker seeds an empty named volume from the image directory,
# ownership included. Skip this and the non-root process cannot write to it.
RUN mkdir -p /data/uploads \
 && chown -R appuser:appuser /data/uploads /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
