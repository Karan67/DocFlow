"""Pure PDF text extraction - no database, no Celery, so it unit-tests easily."""

from __future__ import annotations

from typing import Any, BinaryIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from core.config import settings


class ExtractionError(Exception):
    """Raised for input we cannot process (bad or encrypted PDF)."""


def extract_text_from_pdf(
    fh: BinaryIO, max_chars: int | None = None
) -> dict[str, Any]:
    """Pull raw text out of a PDF and summarise it for the `result` column."""
    limit = settings.MAX_RESULT_TEXT_CHARS if max_chars is None else max_chars

    try:
        reader = PdfReader(fh)
        if reader.is_encrypted:
            # An empty user password is common and decrypts fine; anything else
            # is a genuine failure.
            try:
                if reader.decrypt("") == 0:
                    raise ExtractionError("PDF is password protected")
            except NotImplementedError as exc:
                raise ExtractionError(f"Unsupported PDF encryption: {exc}") from exc

        pages = [(page.extract_text() or "") for page in reader.pages]
    except ExtractionError:
        raise
    except PdfReadError as exc:
        raise ExtractionError(f"Malformed PDF: {exc}") from exc
    except Exception as exc:  # pypdf raises a wide variety of errors
        raise ExtractionError(f"Could not read PDF: {exc}") from exc

    text = "\n\n".join(pages).strip()
    truncated = len(text) > limit

    return {
        "page_count": len(pages),
        "char_count": len(text),
        "truncated": truncated,
        "text": text[:limit],
    }
