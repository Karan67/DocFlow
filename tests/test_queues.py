"""Phase 4: queue routing, priority resolution and rate limiting.

Run inside the worker container:  docker compose exec worker pytest

Two independent axes, deliberately kept separate:

  urgency        -> high / default / low, drained in strict order
  workload class -> ocr, because OCR is ~1000x slower than the other stages

A single worker pool drains high,default,low; a separate pool owns `ocr`
alone. Getting that wrong reintroduces exactly the head-of-line blocking the
split exists to prevent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import redis

from api.limiter import limiter
from api.routes import _resolve_priority
from core.config import settings
from core.models import JobPriority, JobStage, JobStatus
from worker.celery_app import (
    PRIORITY_QUEUES,
    QUEUE_DEFAULT,
    QUEUE_HIGH,
    QUEUE_LOW,
    QUEUE_OCR,
    TASK_EXTRACT_TEXT,
    queue_for,
)

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# routing (pure)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "priority,expected",
    [("high", QUEUE_HIGH), ("normal", QUEUE_DEFAULT), ("low", QUEUE_LOW)],
)
def test_fast_stages_route_by_priority(priority, expected):
    assert queue_for(JobStage.EXTRACT_TEXT.value, priority) == expected
    assert queue_for(JobStage.EMBED.value, priority) == expected


@pytest.mark.parametrize("priority", ["high", "normal", "low"])
def test_ocr_always_uses_its_own_queue(priority):
    """OCR goes to the dedicated pool regardless of urgency.

    Its cost is bounded by CPU rather than queueing: with one OCR worker,
    jumping the queue only reorders a backlog that is saturated either way.
    """
    assert queue_for(JobStage.OCR.value, priority) == QUEUE_OCR


def test_unknown_priority_falls_back_to_default():
    """A value that slipped past the CHECK constraint must not lose the job."""
    assert queue_for(JobStage.EMBED.value, "urgent-ish") == QUEUE_DEFAULT


def test_queue_names_stay_in_step_with_the_enums():
    """`worker.celery_app` uses plain strings so it stays free of the ORM.

    That decoupling is deliberate - the API imports it to enqueue work and
    should not drag SQLAlchemy and pgvector along - but it means nothing keeps
    the two definitions aligned except this test.
    """
    assert set(PRIORITY_QUEUES) == {member.value for member in JobPriority}
    assert queue_for(JobStage.OCR.value, JobPriority.NORMAL.value) == QUEUE_OCR


# --------------------------------------------------------------------------
# priority resolution
# --------------------------------------------------------------------------


def test_explicit_priority_wins_over_size():
    big = settings.LARGE_FILE_BYTES + 1
    assert _resolve_priority(JobPriority.HIGH, big) == "high"


def test_large_uploads_step_aside_by_default():
    """One large scan should not make a queue of small documents wait."""
    assert _resolve_priority(None, settings.LARGE_FILE_BYTES + 1) == "low"


def test_ordinary_uploads_are_normal_priority():
    assert _resolve_priority(None, 1024) == "normal"
    assert _resolve_priority(None, settings.LARGE_FILE_BYTES) == "normal"


# --------------------------------------------------------------------------
# routing through the API and the pipeline
# --------------------------------------------------------------------------


def test_upload_enqueues_on_the_priority_queue(client, cleanup_jobs, unique_pdf):
    client.sent.clear()
    response = client.post(
        "/jobs/upload",
        files={"file": ("urgent.pdf", unique_pdf("high"), "application/pdf")},
        data={"priority": "high"},
    )

    assert response.status_code == 202
    job_id = uuid.UUID(response.json()["job_id"])
    cleanup_jobs.append(job_id)

    _, args, kwargs = _find_send(client, TASK_EXTRACT_TEXT)
    assert args == [str(job_id)]
    assert kwargs["queue"] == QUEUE_HIGH


def test_default_upload_uses_the_default_queue(client, upload, unique_pdf):
    client.sent.clear()
    upload(unique_pdf("normal"))

    _, _, kwargs = _find_send(client, TASK_EXTRACT_TEXT)
    assert kwargs["queue"] == QUEUE_DEFAULT


def test_priority_is_persisted_and_exposed(
    client, cleanup_jobs, read_job, unique_pdf
):
    response = client.post(
        "/jobs/upload",
        files={"file": ("low.pdf", unique_pdf("low"), "application/pdf")},
        data={"priority": "low"},
    )
    assert response.status_code == 202
    job_id = uuid.UUID(response.json()["job_id"])
    cleanup_jobs.append(job_id)

    assert read_job(job_id)["priority"] == "low"
    assert client.get(f"/jobs/{job_id}").json()["priority"] == "low"


def test_invalid_priority_is_rejected(client, unique_pdf):
    response = client.post(
        "/jobs/upload",
        files={"file": ("x.pdf", unique_pdf("bad"), "application/pdf")},
        data={"priority": "immediately"},
    )
    assert response.status_code == 422


def test_scanned_document_hands_off_to_the_ocr_queue(
    client, upload, read_job, scanned_pdf_bytes
):
    """The whole point of the split: a slow scan leaves the fast queues."""
    from worker.tasks import extract_text_task

    job_id = uuid.UUID(upload(scanned_pdf_bytes, "scan.pdf").json()["job_id"])
    client.sent.clear()

    extract_text_task.apply(args=[str(job_id)])

    assert read_job(job_id)["stage"] == "ocr"
    _, _, kwargs = _find_send(client, "docflow.ocr")
    assert kwargs["queue"] == QUEUE_OCR


def test_reaper_requeues_onto_the_right_queue_for_the_stage(
    client, upload, force_job_state, sample_pdf_bytes
):
    """An orphaned OCR job must come back on `ocr`, not on a fast queue."""
    from worker.tasks import reap_stale_jobs

    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    force_job_state(
        job_id,
        status=JobStatus.PROCESSING.value,
        stage="ocr",
        priority="high",
        started_at=datetime.now(timezone.utc)
        - timedelta(seconds=settings.STALE_JOB_SECONDS + 60),
    )
    client.sent.clear()

    reap_stale_jobs.apply().get()

    _, args, kwargs = _find_send(client, "docflow.ocr")
    assert args == [str(job_id)]
    assert kwargs["queue"] == QUEUE_OCR, "priority must not pull OCR onto a fast queue"


def _find_send(client, task_name: str):
    """The recorded send for one task name."""
    matches = [entry for entry in client.sent if entry[0] == task_name]
    assert matches, f"no send recorded for {task_name}; got {client.sent}"
    return matches[-1]


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------


def _clear_rate_limit_window() -> None:
    """Drop the limiter's Redis keys so a test starts from a clean window."""
    connection = redis.from_url(settings.REDIS_URL)
    try:
        for key in connection.scan_iter("LIMITS:*"):
            connection.delete(key)
    finally:
        connection.close()


@pytest.fixture
def rate_limited(monkeypatch):
    """Turn the limiter back on for one test, with a clean window either side."""
    _clear_rate_limit_window()
    monkeypatch.setattr(limiter, "enabled", True)
    yield
    _clear_rate_limit_window()


def test_upload_is_rate_limited(client, cleanup_jobs, rate_limited, sample_pdf_bytes):
    allowed = int(settings.RATE_LIMIT_UPLOAD.split("/")[0])
    statuses = []

    for _ in range(allowed + 3):
        response = client.post(
            "/jobs/upload",
            files={"file": ("rl.pdf", sample_pdf_bytes, "application/pdf")},
        )
        statuses.append(response.status_code)
        if response.status_code in (200, 202):
            cleanup_jobs.append(uuid.UUID(response.json()["job_id"]))

    accepted = [code for code in statuses if code in (200, 202)]
    assert len(accepted) == allowed, "the limit is enforced exactly"
    assert statuses[-1] == 429
    assert statuses.count(429) == 3


def test_rate_limited_response_explains_the_limit(
    client, rate_limited, sample_pdf_bytes
):
    allowed = int(settings.RATE_LIMIT_UPLOAD.split("/")[0])
    for _ in range(allowed):
        client.post(
            "/jobs/upload",
            files={"file": ("rl2.pdf", sample_pdf_bytes, "application/pdf")},
        )

    response = client.post(
        "/jobs/upload",
        files={"file": ("rl2.pdf", sample_pdf_bytes, "application/pdf")},
    )

    assert response.status_code == 429
    assert "Rate limit exceeded" in response.text


def test_reads_are_not_rate_limited(client, rate_limited):
    """Only the expensive endpoint is limited; polling status must stay free."""
    for _ in range(int(settings.RATE_LIMIT_UPLOAD.split("/")[0]) + 5):
        assert client.get("/jobs", params={"limit": 1}).status_code == 200


# --------------------------------------------------------------------------
# trusted proxies
# --------------------------------------------------------------------------


def _request(headers: dict[str, str] | None = None, peer: str = "10.0.0.1"):
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/jobs/upload",
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": (peer, 1234),
            "headers": [
                (key.lower().encode(), value.encode())
                for key, value in (headers or {}).items()
            ],
        }
    )


def test_forwarded_header_is_ignored_without_a_declared_proxy(monkeypatch):
    """Default deployment has no proxy, so the header is not evidence."""
    from api.limiter import client_ip

    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 0)

    assert client_ip(_request({"x-forwarded-for": "1.2.3.4"})) == "10.0.0.1"


def test_forwarded_header_is_read_when_a_proxy_is_declared(monkeypatch):
    from api.limiter import client_ip

    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 1)

    assert client_ip(_request({"x-forwarded-for": "203.0.113.7"})) == "203.0.113.7"


def test_client_supplied_forwarded_entries_cannot_spoof_the_limit(monkeypatch):
    """The header is appended to by each hop; the client controls the left.

    Taking the leftmost entry - the common mistake - would let anyone send a
    random X-Forwarded-For and get a fresh quota on every request.
    """
    from api.limiter import client_ip

    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 1)

    spoofed = _request({"x-forwarded-for": "1.2.3.4, 203.0.113.7"})

    assert client_ip(spoofed) == "203.0.113.7", "must use what the proxy wrote"


def test_falls_back_to_the_socket_when_the_header_is_too_short(monkeypatch):
    """Misconfiguration should not hand out unlimited quota."""
    from api.limiter import client_ip

    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 2)

    assert client_ip(_request({"x-forwarded-for": "203.0.113.7"})) == "10.0.0.1"
