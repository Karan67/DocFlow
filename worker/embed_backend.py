"""Where embedding actually runs.

fastembed keeps the model in-process, which is simple and needs no credentials,
but it is pure CPU: a 4MB PDF is roughly 34 TFLOP of forward passes. A worker
with a fraction of a core cannot do that in any useful time, and the ONNX arenas
alone push resident memory past the limits small hosts impose.

The hosted backend calls the *same* model id over HTTP instead. That is the point
of pinning the model rather than picking whatever an API vendor offers: identical
weights mean an identical vector space, so documents already embedded locally
stay comparable and nothing has to be re-indexed.

Measured on a 12-core desktop, 256 chunks of ~500 characters:

    local    7.7 chunks/s, one core saturated, ~200MB resident
    hosted  91.3 chunks/s, 0.1s of process CPU, ~60MB resident
"""

from __future__ import annotations

import logging
import math
import threading
import time

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _hosted_url() -> str:
    if settings.EMBEDDING_API_URL:
        return settings.EMBEDDING_API_URL
    return (
        "https://router.huggingface.co/hf-inference/models/"
        f"{settings.EMBEDDING_MODEL}/pipeline/feature-extraction"
    )


def _get_client() -> httpx.Client:
    """One pooled client per worker process, built on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                if not settings.EMBEDDING_API_TOKEN:
                    raise RuntimeError(
                        "EMBEDDING_BACKEND=hosted requires EMBEDDING_API_TOKEN."
                    )
                _client = httpx.Client(
                    timeout=settings.EMBEDDING_API_TIMEOUT,
                    headers={
                        "Authorization": f"Bearer {settings.EMBEDDING_API_TOKEN}",
                        "Content-Type": "application/json",
                    },
                )
                logger.info("Hosted embedding backend -> %s", _hosted_url())
    return _client


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return vector if norm == 0 else [v / norm for v in vector]


def _coerce(payload: object, expected: int) -> list[list[float]]:
    """Reduce the response to one vector per input.

    Sentence-transformers endpoints return a vector per input, but some return
    per-token vectors. bge pools on the CLS token, so take index 0 rather than
    averaging - mean pooling here would produce vectors that do not match the
    ones the local backend wrote for already-indexed documents.
    """
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"Unexpected embedding response: {type(payload).__name__}")

    first = payload[0]
    if isinstance(first, list) and first and isinstance(first[0], list):
        logger.warning("Endpoint returned token-level vectors; pooling on CLS token")
        payload = [rows[0] for rows in payload]

    if len(payload) != expected:
        raise RuntimeError(f"Asked for {expected} embeddings, received {len(payload)}")

    return [_l2_normalize([float(x) for x in vec]) for vec in payload]


def _post(batch: list[str]) -> list[list[float]]:
    client = _get_client()
    body = {"inputs": batch, "options": {"wait_for_model": True}}
    delay = 2.0
    last: Exception | None = None

    for attempt in range(1, settings.EMBEDDING_API_RETRIES + 1):
        try:
            response = client.post(_hosted_url(), json=body)
            # 503 means the endpoint is loading the model, 429 is rate limiting.
            # Both are worth waiting out: failing here would fail the whole job
            # when the next attempt would likely have succeeded.
            if response.status_code in (429, 503):
                raise httpx.HTTPStatusError(
                    f"transient {response.status_code}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            return _coerce(response.json(), len(batch))
        except Exception as exc:
            last = exc
            if attempt == settings.EMBEDDING_API_RETRIES:
                break
            logger.warning(
                "Embedding request failed (%d/%d): %s - retrying in %.0fs",
                attempt,
                settings.EMBEDDING_API_RETRIES,
                exc,
                delay,
            )
            time.sleep(delay)
            delay *= 2

    raise RuntimeError(
        f"Embedding endpoint failed after {settings.EMBEDDING_API_RETRIES} attempts: {last}"
    )


def embed_hosted(chunks: list[str]) -> list[list[float]]:
    """Embed via the hosted endpoint, in batches."""
    vectors: list[list[float]] = []
    size = max(1, settings.EMBEDDING_API_BATCH)
    for start in range(0, len(chunks), size):
        vectors.extend(_post(chunks[start : start + size]))
    return vectors


def reset_client() -> None:
    """Drop the pooled client. Used by tests."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None
