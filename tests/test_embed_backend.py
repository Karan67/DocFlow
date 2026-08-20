"""The hosted embedding backend must agree with the local one.

Switching backends is only safe because both point at the same model id. If the
hosted endpoint pooled differently, vectors would land in a different space and
every document already indexed would silently stop matching. That is not
something to assume, so it is asserted here.

The comparison test needs a token and network, so it is opt-in via
EMBEDDING_API_TOKEN. The dispatch and validation tests always run.
"""

from __future__ import annotations

import math
import os

import pytest

from core.config import settings
from worker import embed


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


SAMPLES = [
    "Jon Snow is the bastard son of Eddard Stark, raised at Winterfell.",
    "The invoice total came to 48,200 dollars, payable within thirty days.",
    "short",
]


def test_unknown_backend_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "magic")
    with pytest.raises(embed.EmbeddingError, match="Unknown EMBEDDING_BACKEND"):
        embed.embed_chunks(["anything"])


def test_empty_input_short_circuits(monkeypatch):
    # Must not reach a backend at all - no model load, no network call.
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "magic")
    assert embed.embed_chunks([]) == []


def test_local_backend_produces_correct_dimension(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "local")
    vectors = embed.embed_chunks(SAMPLES)
    assert len(vectors) == len(SAMPLES)
    assert all(len(v) == settings.EMBEDDING_DIM for v in vectors)


@pytest.mark.skipif(
    not os.environ.get("EMBEDDING_API_TOKEN"),
    reason="hosted comparison needs EMBEDDING_API_TOKEN and network access",
)
def test_hosted_matches_local_vector_space(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "local")
    local_vectors = embed.embed_chunks(SAMPLES)

    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "hosted")
    monkeypatch.setattr(
        settings, "EMBEDDING_API_TOKEN", os.environ["EMBEDDING_API_TOKEN"]
    )
    hosted_vectors = embed.embed_chunks(SAMPLES)

    assert len(hosted_vectors) == len(SAMPLES)
    for local_vec, hosted_vec in zip(local_vectors, hosted_vectors):
        assert len(hosted_vec) == settings.EMBEDDING_DIM
        # Anything below this and previously indexed documents would no longer
        # be comparable to newly embedded ones.
        assert _cosine(local_vec, hosted_vec) > 0.999
