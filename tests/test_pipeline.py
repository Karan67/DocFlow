"""Phase 3: OCR routing, embeddings, and multi-stage pipeline behaviour.

Run inside the worker container:  docker compose exec worker pytest

The routing decision is the interesting part. A born-digital PDF has a text
layer and skips OCR entirely; a scan has none and must be recognised. Getting
that wrong in either direction is expensive - running OCR on every document
wastes minutes per file, skipping it on a scan silently produces an empty
document that looks successful.
"""

from __future__ import annotations

import uuid

import pytest

import worker.tasks as tasks_module
from core.config import settings
from core.models import JobStatus
from tests.conftest import PAGE_ONE, SCANNED_LINE
from worker.celery_app import TASK_EXTRACT_TEXT
from worker.embed import chunk_text
from worker.ocr import needs_ocr
from worker.tasks import embed_task, extract_text_task

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# routing decision (pure)
# --------------------------------------------------------------------------


def test_needs_ocr_on_an_empty_text_layer():
    assert needs_ocr(page_count=5, char_count=0) is True


def test_needs_ocr_on_stray_characters():
    """A scan with an embedded logo yields a handful of junk characters."""
    assert needs_ocr(page_count=10, char_count=30) is True


def test_does_not_need_ocr_for_a_real_text_layer():
    assert needs_ocr(page_count=2, char_count=4000) is False


def test_needs_ocr_is_per_page_not_absolute():
    """200 chars is plenty for one page and nowhere near enough for fifty."""
    assert needs_ocr(page_count=1, char_count=200) is False
    assert needs_ocr(page_count=50, char_count=200) is True


def test_needs_ocr_handles_a_zero_page_pdf():
    assert needs_ocr(page_count=0, char_count=0) is False


# --------------------------------------------------------------------------
# chunking (pure)
# --------------------------------------------------------------------------


def test_short_text_is_one_chunk():
    assert chunk_text("a short document") == ["a short document"]


def test_empty_text_produces_no_chunks():
    assert chunk_text("   \n  ") == []


def test_chunks_overlap_so_boundaries_are_not_lost():
    text = "".join(f"{index:04d}." for index in range(400))  # 2000 chars
    chunks = chunk_text(text, chunk_size=500, overlap=100)

    assert len(chunks) > 1
    # Consecutive chunks share their overlap region, so a sentence spanning a
    # cut still appears whole in one of them.
    assert chunks[0][-100:] == chunks[1][:100]


def test_overlap_must_be_smaller_than_chunk_size():
    with pytest.raises(ValueError):
        chunk_text("some text", chunk_size=100, overlap=100)


# --------------------------------------------------------------------------
# pipeline routing
# --------------------------------------------------------------------------


def test_digital_pdf_skips_ocr(
    upload, run_pipeline, fake_embedder, read_job, sample_pdf_bytes
):
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])

    run_pipeline(job_id)

    job = read_job(job_id)
    assert job["status"] == JobStatus.DONE.value
    assert [entry["stage"] for entry in job["stages"]] == ["extract_text", "embed"]
    assert job["result"]["source"] == "text_layer"
    assert job["result"]["needed_ocr"] is False
    assert PAGE_ONE in job["result"]["text"]


def test_scanned_pdf_is_routed_through_ocr(
    upload, run_pipeline, fake_embedder, read_job, scanned_pdf_bytes
):
    job_id = uuid.UUID(upload(scanned_pdf_bytes, "scan.pdf").json()["job_id"])

    run_pipeline(job_id)

    job = read_job(job_id)
    assert job["status"] == JobStatus.DONE.value
    assert [entry["stage"] for entry in job["stages"]] == [
        "extract_text",
        "ocr",
        "embed",
    ]
    assert job["result"]["source"] == "ocr"
    assert job["result"]["needed_ocr"] is True
    # The text layer was empty; everything below came from recognition.
    assert job["result"]["text_layer_chars"] == 0
    # OCR is imperfect, so match on a distinctive word rather than the sentence.
    assert "quarterly" in job["result"]["text"].lower()


def test_stage_log_records_timing_and_attempts(
    upload, run_pipeline, fake_embedder, read_job, sample_pdf_bytes
):
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])

    run_pipeline(job_id)

    for entry in read_job(job_id)["stages"]:
        assert entry["status"] == JobStatus.DONE.value
        assert entry["duration_ms"] >= 0
        assert entry["detail"]["attempts"] == 1
        assert entry["at"]


def test_stale_message_for_a_passed_stage_is_ignored(
    upload, fake_embedder, read_job, sample_pdf_bytes
):
    """Under acks_late an old stage message can be redelivered after the move on."""
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    extract_text_task.apply(args=[str(job_id)])
    assert read_job(job_id)["stage"] == "embed"

    outcome = extract_text_task.apply(args=[str(job_id)]).get()

    assert outcome["reason"] == "wrong_stage"
    assert read_job(job_id)["stage"] == "embed", "the job was not dragged backwards"


def test_advancing_a_stage_enqueues_the_next_one(
    client, upload, fake_embedder, sample_pdf_bytes
):
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    client.sent.clear()

    extract_text_task.apply(args=[str(job_id)])

    # Committed first, enqueued second - same rule as the upload route.
    assert client.sent == [("docflow.embed", [str(job_id)], {"queue": "default"})]


# --------------------------------------------------------------------------
# embeddings
# --------------------------------------------------------------------------


def test_embeddings_are_written_with_the_schema_dimension(
    upload, run_pipeline, read_job, read_chunks, sample_pdf_bytes
):
    """Uses the real model - the dimension has to match the vector(N) column."""
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])

    run_pipeline(job_id)

    job = read_job(job_id)
    assert job["status"] == JobStatus.DONE.value
    assert job["result"]["embedded"] is True
    assert job["result"]["embedding_model"] == settings.EMBEDDING_MODEL

    chunks = read_chunks(job_id)
    assert len(chunks) == job["result"]["chunk_count"] >= 1
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    assert all(c["dimensions"] == settings.EMBEDDING_DIM for c in chunks)
    assert PAGE_ONE in chunks[0]["content"]


def test_rerunning_embed_replaces_chunks_rather_than_colliding(
    upload, run_pipeline, force_job_state, read_chunks, fake_embedder, sample_pdf_bytes
):
    """acks_late can redeliver a finished stage; it must be safe to re-run."""
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    run_pipeline(job_id)
    first = read_chunks(job_id)
    assert first

    # Put the job back on the embed stage, as a redelivery would find it.
    force_job_state(
        job_id, status=JobStatus.PENDING.value, stage="embed", completed_at=None
    )
    embed_task.apply(args=[str(job_id)])

    second = read_chunks(job_id)
    assert len(second) == len(first), "no duplicate rows, no unique violation"
    assert [c["content"] for c in second] == [c["content"] for c in first]


def test_document_with_no_text_finishes_without_embedding(
    upload, force_job_state, read_job, read_chunks, fake_embedder, sample_pdf_bytes
):
    """A blank scan is a legitimate outcome, not a failure."""
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    force_job_state(job_id, stage="embed", result={"text": "   "})

    embed_task.apply(args=[str(job_id)])

    job = read_job(job_id)
    assert job["status"] == JobStatus.DONE.value
    assert job["result"]["embedded"] is False
    assert job["result"]["chunk_count"] == 0
    assert read_chunks(job_id) == []


def test_chunks_are_deleted_with_their_job(
    upload, run_pipeline, read_chunks, fake_embedder, sample_pdf_bytes, cleanup_jobs
):
    from core.database import session_scope
    from core.models import Job

    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    run_pipeline(job_id)
    assert read_chunks(job_id)

    with session_scope() as session:
        session.delete(session.get(Job, job_id))
    cleanup_jobs.remove(job_id)

    assert read_chunks(job_id) == [], "ON DELETE CASCADE leaves no orphan vectors"


# --------------------------------------------------------------------------
# mid-pipeline failure
# --------------------------------------------------------------------------


def test_failure_in_a_later_stage_is_attributed_to_that_stage(
    upload, run_pipeline, read_job, monkeypatch, sample_pdf_bytes
):
    """The job status says it failed; the stage log says where."""

    def exploding_embed(text):
        raise ConnectionError("vector store unreachable")

    monkeypatch.setattr(tasks_module, "embed_document", exploding_embed)
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])

    run_pipeline(job_id)

    job = read_job(job_id)
    assert job["status"] == JobStatus.DEAD_LETTER.value
    assert job["stage"] == "embed"

    stages = {entry["stage"]: entry for entry in job["stages"]}
    assert stages["extract_text"]["status"] == JobStatus.DONE.value
    assert stages["embed"]["status"] == JobStatus.DEAD_LETTER.value
    assert "ConnectionError" in stages["embed"]["detail"]["error"]
    # Extraction's output survives the later failure, so the work is not lost.
    assert PAGE_ONE in job["result"]["text"]


def test_reaper_requeues_the_stage_the_job_is_actually_on(
    client, upload, force_job_state, fake_embedder, read_job, sample_pdf_bytes
):
    """A job orphaned at embed must be requeued as embed, not from the start."""
    from datetime import datetime, timedelta, timezone

    from worker.tasks import reap_stale_jobs

    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    force_job_state(
        job_id,
        status=JobStatus.PROCESSING.value,
        stage="embed",
        started_at=datetime.now(timezone.utc)
        - timedelta(seconds=settings.STALE_JOB_SECONDS + 60),
    )
    client.sent.clear()

    reap_stale_jobs.apply().get()

    assert ("docflow.embed", [str(job_id)], {"queue": "default"}) in client.sent
    assert (TASK_EXTRACT_TEXT, [str(job_id)], {"queue": "default"}) not in client.sent
    assert read_job(job_id)["stage"] == "embed"
