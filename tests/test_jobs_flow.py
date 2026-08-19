"""End-to-end Phase 1 flow against a live Postgres.

Run inside the worker container:  docker compose exec worker pytest

Note: Celery's `task_always_eager` has **no effect on `app.send_task()`** - it
only short-circuits `Task.apply_async()`. So the producer side is verified by
capturing the send (see the `client` fixture), and the consumer side by
invoking the task directly with `.apply()`. That also keeps the two halves
tested independently, which is how they actually run.
"""

from __future__ import annotations

import uuid

import pytest

from core.models import JobStatus
from tests.conftest import PAGE_ONE
from worker.celery_app import TASK_EXTRACT_TEXT
from worker.tasks import extract_text_task

pytestmark = pytest.mark.integration


def test_upload_creates_pending_job_and_enqueues(
    client, upload, read_job, sample_pdf_bytes
):
    response = upload(sample_pdf_bytes)

    assert response.status_code == 202
    body = response.json()
    job_id = uuid.UUID(body["job_id"])
    assert body["status"] == JobStatus.PENDING.value
    assert body["status_url"] == f"/jobs/{job_id}"
    assert body["deduplicated"] is False

    # The row exists and is committed *before* the enqueue happens.
    job = read_job(job_id)
    assert job["status"] == JobStatus.PENDING.value
    assert job["stage"] == "extract_text", "pipelines always start at extraction"
    assert job["stages"] == []
    assert job["started_at"] is None

    assert client.sent == [(TASK_EXTRACT_TEXT, [str(job_id)], {"queue": "default"})]


def test_worker_completes_the_job(
    client, upload, run_pipeline, fake_embedder, sample_pdf_bytes
):
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])

    run_pipeline(job_id)

    detail = client.get(f"/jobs/{job_id}").json()
    assert detail["status"] == JobStatus.DONE.value
    assert detail["started_at"] is not None
    assert detail["completed_at"] is not None
    assert detail["error_message"] is None
    assert detail["result"]["page_count"] == 2
    assert PAGE_ONE in detail["result"]["text"]
    assert [entry["stage"] for entry in detail["stages"]] == ["extract_text", "embed"]


def test_second_delivery_is_ignored(
    upload, run_pipeline, fake_embedder, sample_pdf_bytes
):
    """Re-delivery must not reprocess a settled job - required by acks_late."""
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])

    run_pipeline(job_id)
    second = extract_text_task.apply(args=[str(job_id)]).get()

    assert second["reason"] == "already_handled"
    assert second["status"] == JobStatus.DONE.value


def test_unreadable_file_marks_job_failed(client, upload):
    job_id = uuid.UUID(upload(b"%PDF-1.4 truncated garbage", "broken.pdf").json()["job_id"])

    outcome = extract_text_task.apply(args=[str(job_id)]).get()
    assert outcome["status"] == JobStatus.FAILED.value

    detail = client.get(f"/jobs/{job_id}").json()
    assert detail["status"] == JobStatus.FAILED.value
    assert detail["error_message"]
    assert detail["completed_at"] is not None


def test_missing_job_returns_404(client):
    assert client.get(f"/jobs/{uuid.uuid4()}").status_code == 404


def test_rejects_non_pdf_extension(client):
    response = client.post(
        "/jobs/upload", files={"file": ("notes.txt", b"hello", "text/plain")}
    )
    assert response.status_code == 415


def test_rejects_empty_file(client):
    response = client.post(
        "/jobs/upload", files={"file": ("empty.pdf", b"", "application/pdf")}
    )
    assert response.status_code == 400


def test_list_endpoint_returns_the_new_job(client, upload, sample_pdf_bytes):
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])

    body = client.get("/jobs", params={"limit": 100}).json()
    assert body["total"] >= 1
    assert str(job_id) in {item["id"] for item in body["items"]}
    # Summaries stay small - no result payload.
    assert "result" not in body["items"][0]
