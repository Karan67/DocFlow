"""Text chunking and embedding generation.

fastembed runs the model as ONNX on CPU. That keeps the worker image in the
hundreds of megabytes rather than the gigabytes a torch-based stack would need,
which matters when the image ships to EC2 in Phase 6.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastembed import TextEmbedding

from core.config import settings

logger = logging.getLogger(__name__)

_model: TextEmbedding | None = None
_model_lock = threading.Lock()


class EmbeddingError(Exception):
    """Raised when embeddings cannot be produced for otherwise valid text."""


def get_model() -> TextEmbedding:
    """Load the model once per worker process.

    Each prefork child loads its own copy on first use. The model is baked into
    the image, so this is a disk read rather than a download.
    """
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                logger.info("Loading embedding model %s", settings.EMBEDDING_MODEL)
                _model = TextEmbedding(
                    model_name=settings.EMBEDDING_MODEL,
                    cache_dir=None,
                )
                logger.info("Embedding model ready")
    return _model


def chunk_text(
    text: str, chunk_size: int | None = None, overlap: int | None = None
) -> list[str]:
    """Split text into overlapping windows.

    Overlap exists so a sentence spanning a boundary still appears whole in one
    of the two chunks; without it, retrieval misses facts that straddle a cut.
    """
    size = chunk_size if chunk_size is not None else settings.CHUNK_SIZE
    lap = overlap if overlap is not None else settings.CHUNK_OVERLAP

    if size <= 0:
        raise ValueError("chunk_size must be positive")
    if lap >= size:
        raise ValueError("overlap must be smaller than chunk_size")

    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= size:
        return [cleaned]

    step = size - lap
    chunks: list[str] = []
    for start in range(0, len(cleaned), step):
        window = cleaned[start : start + size].strip()
        if window:
            chunks.append(window)
        if start + size >= len(cleaned):
            break
    return chunks


def embed_chunks(chunks: list[str]) -> list[list[float]]:
    """Embed a list of chunks, validating the dimension against the schema."""
    if not chunks:
        return []

    try:
        vectors = [list(map(float, v)) for v in get_model().embed(chunks)]
    except Exception as exc:
        raise EmbeddingError(f"Embedding failed: {type(exc).__name__}: {exc}") from exc

    if len(vectors) != len(chunks):
        raise EmbeddingError(
            f"Model returned {len(vectors)} vectors for {len(chunks)} chunks"
        )

    # A dimension mismatch would otherwise surface as an opaque insert error
    # against the vector(N) column, far from the actual cause.
    for vector in vectors:
        if len(vector) != settings.EMBEDDING_DIM:
            raise EmbeddingError(
                f"Model {settings.EMBEDDING_MODEL} returned {len(vector)} "
                f"dimensions, but the schema expects {settings.EMBEDDING_DIM}. "
                "EMBEDDING_MODEL and EMBEDDING_DIM must agree."
            )
    return vectors


def embed_document(text: str) -> dict[str, Any]:
    """Chunk and embed a document's text."""
    chunks = chunk_text(text)
    vectors = embed_chunks(chunks)
    return {
        "chunks": chunks,
        "vectors": vectors,
        "chunk_count": len(chunks),
        "model": settings.EMBEDDING_MODEL,
        "dimensions": settings.EMBEDDING_DIM,
    }
