from __future__ import annotations

import io

import pytest
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

PAGE_ONE = "DocFlow phase one smoke test."
PAGE_TWO = "Second page of the sample document."


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    """A real two-page PDF with extractable text."""
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=LETTER)
    pdf.drawString(72, 720, PAGE_ONE)
    pdf.showPage()
    pdf.drawString(72, 720, PAGE_TWO)
    pdf.showPage()
    pdf.save()
    return buf.getvalue()
