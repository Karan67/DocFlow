from __future__ import annotations

import io
import textwrap
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pdf2image import convert_from_bytes
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from api.limiter import limiter
from api.main import app
from core.database import session_scope
from core.models import TERMINAL_STATUSES, DocumentChunk, Job
from core.storage import get_storage
from worker.celery_app import celery_app

PAGE_ONE = "DocFlow phase one smoke test."
PAGE_TWO = "Second page of the sample document."
OTHER_PAGE = "A completely different document, with different bytes."
SCANNED_LINE = "Scanned quarterly report page with no text layer at all."


#: Body text so each fixture page has a realistic character density. A page
#: carrying a single short line averages below OCR_MIN_CHARS_PER_PAGE and gets
#: correctly classified as a scan - which is right for the threshold and wrong
#: for a fixture that is meant to represent a born-digital document.
_FILLER = (
    "This paragraph gives the page a realistic amount of text. A page of a "
    "born-digital document carries hundreds of characters, which is exactly "
    "what separates it from a scan whose text layer is empty."
)


def _build_pdf(*lines: str) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=LETTER)
    for line in lines:
        pdf.drawString(72, 720, line)
        y = 700
        for wrapped in textwrap.wrap(_FILLER, 90):
            pdf.drawString(72, y, wrapped)
            y -= 16
        pdf.showPage()
    pdf.save()
    return buf.getvalue()


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    """A real two-page PDF with extractable text."""
    return _build_pdf(PAGE_ONE, PAGE_TWO)


@pytest.fixture
def unique_pdf():
    """Build a PDF whose bytes are unique, so it cannot dedupe onto another test.

    Deduplication is keyed on the content hash, so two tests uploading
    identical bytes would share a job and quietly assert about each other.
    """

    def _make(label: str = "doc") -> bytes:
        return _build_pdf(f"{label} {uuid.uuid4()}", PAGE_TWO)

    return _make


@pytest.fixture
def other_pdf_bytes() -> bytes:
    """A different PDF, so it hashes to a different idempotency key."""
    return _build_pdf(OTHER_PAGE)


@pytest.fixture(scope="session")
def scanned_pdf_bytes() -> bytes:
    """An image-only PDF - what a scanner produces. No text layer at all.

    Built by rasterising a real PDF, which is exactly how a scan differs from a
    born-digital file. Session-scoped because rendering is not free.
    """
    images = convert_from_bytes(_build_pdf(SCANNED_LINE), dpi=150)
    buf = io.BytesIO()
    images[0].save(
        buf, "PDF", resolution=150.0, save_all=True, append_images=images[1:]
    )
    for image in images:
        image.close()
    return buf.getvalue()


@pytest.fixture
def client(monkeypatch):
    """TestClient with the broker stubbed out; records what would be enqueued."""
    sent: list[tuple] = []

    def fake_send_task(name, args=None, **kwargs):
        sent.append((name, args, kwargs))
        return SimpleNamespace(id=str(uuid.uuid4()))

    monkeypatch.setattr(celery_app, "send_task", fake_send_task)
    # The limiter is Redis-backed and its window outlives a single test, so a
    # suite that trips it would fail every later test. Tests that care about
    # rate limiting turn it back on and clear the window themselves.
    monkeypatch.setattr(limiter, "enabled", False)
    with TestClient(app) as test_client:
        test_client.sent = sent  # type: ignore[attr-defined]
        yield test_client


@pytest.fixture
def cleanup_jobs():
    """Job ids to delete (with their stored files) when the test finishes."""
    created: list[uuid.UUID] = []
    yield created
    storage = get_storage()
    with session_scope() as session:
        for job_id in created:
            job = session.get(Job, job_id)
            if job is not None:
                storage.delete(job.file_path)
                session.delete(job)  # chunks cascade


@pytest.fixture
def upload(client, cleanup_jobs):
    """POST a PDF to the upload endpoint and register it for cleanup."""

    def _upload(pdf_bytes: bytes, filename: str = "sample.pdf"):
        response = client.post(
            "/jobs/upload",
            files={"file": (filename, pdf_bytes, "application/pdf")},
        )
        if response.status_code in (200, 202):
            job_id = uuid.UUID(response.json()["job_id"])
            if job_id not in cleanup_jobs:
                cleanup_jobs.append(job_id)
        return response

    return _upload


@pytest.fixture
def run_pipeline():
    """Drive a job through every remaining stage synchronously.

    The producer/consumer split means each stage enqueues the next rather than
    calling it, and `send_task` is stubbed in tests - so the test has to walk
    the stages itself, which is also a useful check that `stage` always points
    at real, runnable work.
    """
    from worker.tasks import STAGE_TASKS, embed_task, extract_text_task, ocr_task

    by_name = {
        STAGE_TASKS["extract_text"]: extract_text_task,
        STAGE_TASKS["ocr"]: ocr_task,
        STAGE_TASKS["embed"]: embed_task,
    }

    def _run(job_id: uuid.UUID, max_steps: int = 6) -> None:
        for _ in range(max_steps):
            with session_scope() as session:
                job = session.get(Job, job_id)
                if job is None or job.status in TERMINAL_STATUSES:
                    return
                stage = job.stage
            by_name[STAGE_TASKS[stage]].apply(args=[str(job_id)])
        raise AssertionError(f"job {job_id} did not settle within {max_steps} stages")

    return _run


@pytest.fixture
def fake_embedder(monkeypatch):
    """Stub out embedding so pipeline tests do not pay the model load.

    Tests that care about real vectors use the model directly instead.
    """
    import worker.tasks as tasks_module
    from core.config import settings

    def fake_embed_document(text: str) -> dict[str, Any]:
        chunks = [text[i : i + 400] for i in range(0, len(text), 400)] or [""]
        return {
            "chunks": chunks,
            "vectors": [[0.01] * settings.EMBEDDING_DIM for _ in chunks],
            "chunk_count": len(chunks),
            "model": "stub",
            "dimensions": settings.EMBEDDING_DIM,
        }

    monkeypatch.setattr(tasks_module, "embed_document", fake_embed_document)
    return fake_embed_document


@pytest.fixture
def force_job_state():
    """Push a job directly into a given state, bypassing the state machine."""

    def _force(job_id: uuid.UUID, **fields: Any) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            assert job is not None, f"no job {job_id}"
            for name, value in fields.items():
                setattr(job, name, value)

    return _force


@pytest.fixture
def read_job():
    """Fetch a fresh copy of a job's fields as a plain dict."""

    def _read(job_id: uuid.UUID) -> dict[str, Any]:
        with session_scope() as session:
            job = session.get(Job, job_id)
            assert job is not None, f"no job {job_id}"
            return {
                "status": job.status,
                "stage": job.stage,
                "priority": job.priority,
                "stages": list(job.stages or []),
                "retry_count": job.retry_count,
                "max_retries": job.max_retries,
                "result": job.result,
                "error_message": job.error_message,
                "started_at": job.started_at,
                "completed_at": job.completed_at,
                "next_retry_at": job.next_retry_at,
                "idempotency_key": job.idempotency_key,
            }

    return _read


@pytest.fixture
def read_chunks():
    """Fetch the stored chunks for a job, ordered."""

    def _read(job_id: uuid.UUID) -> list[dict[str, Any]]:
        with session_scope() as session:
            rows = (
                session.query(DocumentChunk)
                .filter(DocumentChunk.job_id == job_id)
                .order_by(DocumentChunk.chunk_index)
                .all()
            )
            return [
                {
                    "chunk_index": row.chunk_index,
                    "content": row.content,
                    "dimensions": len(row.embedding),
                }
                for row in rows
            ]

    return _read
