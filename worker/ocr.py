"""OCR for PDFs with no usable text layer.

Pure functions - no database, no Celery - so this unit-tests without a stack.

OCR is the slow path by orders of magnitude: reading a text layer is
microseconds per page, rendering and recognising an image is seconds. That
asymmetry is why it is a separate pipeline stage rather than a fallback buried
inside extraction, and why page count is capped.
"""

from __future__ import annotations

import logging
from typing import Any, BinaryIO

import pytesseract
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFPageCountError, PDFSyntaxError

from core.config import settings
from worker.extract import ExtractionError

logger = logging.getLogger(__name__)


def needs_ocr(page_count: int, char_count: int) -> bool:
    """True when the text layer is too sparse to be real content.

    A born-digital PDF yields hundreds of characters per page. A scan yields
    zero, or a handful of stray ligatures from an embedded logo.
    """
    if page_count <= 0:
        return False
    return (char_count / page_count) < settings.OCR_MIN_CHARS_PER_PAGE


def ocr_pdf(fh: BinaryIO, max_chars: int | None = None) -> dict[str, Any]:
    """Render each page to an image and run tesseract over it."""
    limit = settings.MAX_RESULT_TEXT_CHARS if max_chars is None else max_chars
    payload = fh.read()

    try:
        images = convert_from_bytes(
            payload,
            dpi=settings.OCR_DPI,
            first_page=1,
            last_page=settings.OCR_MAX_PAGES,
        )
    except (PDFPageCountError, PDFSyntaxError) as exc:
        raise ExtractionError(f"Could not render PDF for OCR: {exc}") from exc
    except Exception as exc:
        # pdf2image surfaces a missing poppler binary as a generic OSError.
        raise ExtractionError(f"PDF rendering failed: {exc}") from exc

    pages: list[str] = []
    try:
        for index, image in enumerate(images, start=1):
            text = pytesseract.image_to_string(image, lang=settings.OCR_LANGUAGE)
            pages.append(text or "")
            logger.debug("OCR page %s produced %s chars", index, len(text or ""))
    except pytesseract.TesseractNotFoundError as exc:  # pragma: no cover
        raise ExtractionError(f"Tesseract is not installed: {exc}") from exc
    finally:
        for image in images:
            image.close()

    text = "\n\n".join(pages).strip()
    truncated = len(text) > limit

    return {
        "page_count": len(pages),
        "char_count": len(text),
        "truncated": truncated,
        "text": text[:limit],
        "source": "ocr",
        "ocr_dpi": settings.OCR_DPI,
        "ocr_language": settings.OCR_LANGUAGE,
        # Flag when the cap stopped us short, so a partial result is not
        # silently mistaken for the whole document.
        "pages_capped": len(images) >= settings.OCR_MAX_PAGES,
    }
