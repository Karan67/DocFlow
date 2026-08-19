FROM python:3.12-slim

# Celery 5.x does not yet officially support Python 3.14; the image pins 3.12
# so the local interpreter version is irrelevant.

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# curl is used by the compose healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY . .

# Create the upload directory, owned by appuser, BEFORE the named volume is
# first mounted: Docker seeds an empty named volume from the image directory,
# ownership included. Skip this and the non-root process cannot write to it.
RUN useradd --create-home --uid 1000 appuser \
 && mkdir -p /data/uploads \
 && chown -R appuser:appuser /data/uploads /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
