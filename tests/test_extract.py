"""Unit tests for the extraction logic - no database, no broker."""

from __future__ import annotations

import io

import pytest

from tests.conftest import PAGE_ONE, PAGE_TWO
from worker.extract import ExtractionError, extract_text_from_pdf


def test_extracts_every_page(sample_pdf_bytes: bytes) -> None:
    result = extract_text_from_pdf(io.BytesIO(sample_pdf_bytes))

    assert result["page_count"] == 2
    assert PAGE_ONE in result["text"]
    assert PAGE_TWO in result["text"]
    assert result["truncated"] is False
    assert result["char_count"] == len(result["text"])


def test_truncates_long_text(sample_pdf_bytes: bytes) -> None:
    result = extract_text_from_pdf(io.BytesIO(sample_pdf_bytes), max_chars=10)

    assert result["truncated"] is True
    assert len(result["text"]) == 10
    # char_count reports the true length, not the stored length.
    assert result["char_count"] > 10


def test_rejects_non_pdf() -> None:
    with pytest.raises(ExtractionError):
        extract_text_from_pdf(io.BytesIO(b"this is definitely not a pdf"))
