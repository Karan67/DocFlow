"""Phase 2: retries with backoff, dead-lettering, idempotency and the reaper.

Run inside the api container:  docker compose exec api pytest

A note on how the retry tests work. Under `.apply()` Celery runs the task
eagerly, and `self.retry()` in eager mode re-executes the task inline rather
than scheduling it on the broker. So a single `.apply()` call drives the whole
retry chain synchronously, with countdowns skipped - which is exactly what we
want in a test. The DB is the assertion target, not the return value.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import worker.tasks as tasks_module
from core.config import settings
from core.models import JobStatus
from worker.celery_app import TASK_EXTRACT_TEXT
from worker.extract import ExtractionError
from worker.tasks import _backoff_seconds, extract_text_task, reap_stale_jobs

pytestmark = pytest.mark.integration


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _long_ago() -> datetime:
    return _now() - timedelta(seconds=settings.STALE_JOB_SECONDS + 60)


@pytest.fixture
def failing_extractor(monkeypatch):
    """Swap extraction for something that fails a controlled number of times."""

    def _install(failures: int, exc: Exception | None = None) -> dict:
        state = {"calls": 0}
        error = exc or ConnectionError("database went away")

        def fake_extract(fh, max_chars=None):
            state["calls"] += 1
            if state["calls"] <= failures:
                raise error
            # Dense enough to stay above OCR_MIN_CHARS_PER_PAGE, so these
            # tests exercise the retry path rather than the OCR routing path.
            text = "Recovered document text. " * 20
            return {
                "page_count": 1,
                "char_count": len(text),
                "truncated": False,
                "text": text,
            }

        monkeypatch.setattr(tasks_module, "extract_text_from_pdf", fake_extract)
        return state

    return _install


# --------------------------------------------------------------------------
# retry and dead-letter
# --------------------------------------------------------------------------


def test_transient_failure_retries_then_succeeds(
    upload, read_job, sample_pdf_bytes, failing_extractor
):
    state = failing_extractor(failures=2)
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])

    extract_text_task.apply(args=[str(job_id)])

    job = read_job(job_id)
    assert state["calls"] == 3, "two failures then one success"
    # The stage succeeded, so the pipeline advanced rather than finishing.
    assert job["status"] == JobStatus.PENDING.value
    assert job["stage"] == "embed"
    # Each stage gets a fresh budget, so retry_count resets on advance - the
    # attempt count survives in the stage log instead.
    assert job["retry_count"] == 0
    assert job["stages"][0]["detail"]["attempts"] == 3
    # A recovered stage carries no error and no pending retry.
    assert job["error_message"] is None
    assert job["next_retry_at"] is None


def test_transient_failure_dead_letters_after_max_retries(
    upload, read_job, sample_pdf_bytes, failing_extractor
):
    state = failing_extractor(failures=99)
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])

    extract_text_task.apply(args=[str(job_id)])

    job = read_job(job_id)
    assert job["status"] == JobStatus.DEAD_LETTER.value
    assert job["retry_count"] == job["max_retries"] == 3
    # One initial attempt plus three retries.
    assert state["calls"] == 4
    assert "ConnectionError" in job["error_message"]
    assert job["completed_at"] is not None
    assert job["next_retry_at"] is None


def test_permanent_failure_is_not_retried(
    upload, read_job, sample_pdf_bytes, failing_extractor
):
    """Bad input fails once and stops - retrying it would just burn the budget."""
    state = failing_extractor(failures=99, exc=ExtractionError("PDF is password protected"))
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])

    extract_text_task.apply(args=[str(job_id)])

    job = read_job(job_id)
    assert job["status"] == JobStatus.FAILED.value, "FAILED, not DEAD_LETTER"
    assert state["calls"] == 1, "no retries were attempted"
    assert job["retry_count"] == 0
    assert "password protected" in job["error_message"]


def test_dead_letter_is_queryable_separately_from_failed(
    client, upload, read_job, sample_pdf_bytes, failing_extractor
):
    failing_extractor(failures=99)
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    extract_text_task.apply(args=[str(job_id)])

    body = client.get("/jobs", params={"status": "DEAD_LETTER", "limit": 100}).json()
    assert str(job_id) in {item["id"] for item in body["items"]}

    failed = client.get("/jobs", params={"status": "FAILED", "limit": 100}).json()
    assert str(job_id) not in {item["id"] for item in failed["items"]}


# --------------------------------------------------------------------------
# backoff
# --------------------------------------------------------------------------


def test_backoff_grows_exponentially_and_is_capped(monkeypatch):
    monkeypatch.setattr(settings, "RETRY_JITTER", False)

    delays = [_backoff_seconds(n) for n in range(5)]
    assert delays == sorted(delays) and delays[0] < delays[-1]
    assert delays[1] == delays[0] * 2
    assert _backoff_seconds(100) == settings.RETRY_BACKOFF_MAX


def test_backoff_jitter_spreads_simultaneous_retries():
    # Same attempt number, many draws: jitter must not return one fixed value,
    # or a burst of failures would all retry at the same instant.
    draws = {_backoff_seconds(6) for _ in range(30)}
    assert len(draws) > 1


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


def test_identical_upload_is_deduplicated(upload, sample_pdf_bytes):
    first = upload(sample_pdf_bytes)
    second = upload(sample_pdf_bytes)

    assert first.status_code == 202
    assert first.json()["deduplicated"] is False

    assert second.status_code == 200, "nothing new was accepted"
    assert second.json()["deduplicated"] is True
    assert second.json()["job_id"] == first.json()["job_id"]


def test_idempotency_key_is_the_content_hash(upload, read_job, sample_pdf_bytes):
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    assert read_job(job_id)["idempotency_key"] == hashlib.sha256(sample_pdf_bytes).hexdigest()


def test_different_content_creates_separate_jobs(
    upload, sample_pdf_bytes, other_pdf_bytes
):
    first = upload(sample_pdf_bytes)
    second = upload(other_pdf_bytes, "other.pdf")

    assert second.status_code == 202
    assert second.json()["deduplicated"] is False
    assert first.json()["job_id"] != second.json()["job_id"]


def test_reupload_after_failure_creates_a_new_job(upload, read_job):
    """The partial unique index frees the key once a job has failed."""
    broken = b"%PDF-1.4 truncated garbage"
    first = upload(broken, "broken.pdf")
    first_id = uuid.UUID(first.json()["job_id"])
    extract_text_task.apply(args=[str(first_id)])
    assert read_job(first_id)["status"] == JobStatus.FAILED.value

    second = upload(broken, "broken.pdf")
    assert second.status_code == 202
    assert second.json()["job_id"] != first.json()["job_id"]


# --------------------------------------------------------------------------
# reclaiming orphaned work
# --------------------------------------------------------------------------


def test_stale_processing_job_is_reclaimed(
    upload, force_job_state, read_job, sample_pdf_bytes
):
    """A redelivered task takes over work whose worker died."""
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    force_job_state(
        job_id, status=JobStatus.PROCESSING.value, started_at=_long_ago()
    )

    extract_text_task.apply(args=[str(job_id)])

    job = read_job(job_id)
    assert job["status"] == JobStatus.PENDING.value
    assert job["stage"] == "embed", "the reclaimed stage completed and advanced"
    assert job["stages"][0]["detail"]["attempts"] == 2, (
        "the reclaim costs a retry, bounding redelivery"
    )


def test_in_flight_job_is_not_stolen(
    upload, force_job_state, read_job, sample_pdf_bytes
):
    """Duplicate delivery while another worker is genuinely running the job."""
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    force_job_state(job_id, status=JobStatus.PROCESSING.value, started_at=_now())

    outcome = extract_text_task.apply(args=[str(job_id)]).get()

    assert outcome["reason"] == "in_flight"
    assert read_job(job_id)["status"] == JobStatus.PROCESSING.value


def test_orphaned_job_out_of_retries_is_dead_lettered(
    upload, force_job_state, read_job, sample_pdf_bytes
):
    """A task that reliably kills its worker cannot be redelivered forever."""
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    force_job_state(
        job_id,
        status=JobStatus.PROCESSING.value,
        started_at=_long_ago(),
        retry_count=3,
        max_retries=3,
    )

    outcome = extract_text_task.apply(args=[str(job_id)]).get()

    assert outcome["reason"] == "reclaim_budget_exhausted"
    assert read_job(job_id)["status"] == JobStatus.DEAD_LETTER.value


# --------------------------------------------------------------------------
# the reaper
# --------------------------------------------------------------------------


def test_reaper_requeues_orphaned_jobs(
    client, upload, force_job_state, read_job, sample_pdf_bytes
):
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    force_job_state(
        job_id, status=JobStatus.PROCESSING.value, started_at=_long_ago()
    )
    client.sent.clear()

    summary = reap_stale_jobs.apply().get()

    assert summary["requeued"] >= 1
    job = read_job(job_id)
    assert job["status"] == JobStatus.RETRYING.value
    assert job["retry_count"] == 1
    assert job["next_retry_at"] is not None
    # Committed first, enqueued second - same rule as the upload route.
    assert (TASK_EXTRACT_TEXT, [str(job_id)], {"queue": "default"}) in client.sent

    detail = client.get(f"/jobs/{job_id}").json()
    assert detail["status"] == JobStatus.RETRYING.value
    assert detail["next_retry_at"] is not None


def test_reaper_dead_letters_when_budget_is_exhausted(
    client, upload, force_job_state, read_job, sample_pdf_bytes
):
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    force_job_state(
        job_id,
        status=JobStatus.PROCESSING.value,
        started_at=_long_ago(),
        retry_count=3,
        max_retries=3,
    )
    client.sent.clear()

    summary = reap_stale_jobs.apply().get()

    assert summary["dead_lettered"] >= 1
    job = read_job(job_id)
    assert job["status"] == JobStatus.DEAD_LETTER.value
    assert job["completed_at"] is not None
    assert (TASK_EXTRACT_TEXT, [str(job_id)], {"queue": "default"}) not in client.sent


def test_reaper_leaves_healthy_jobs_alone(
    client, upload, force_job_state, read_job, sample_pdf_bytes
):
    job_id = uuid.UUID(upload(sample_pdf_bytes).json()["job_id"])
    force_job_state(job_id, status=JobStatus.PROCESSING.value, started_at=_now())

    reap_stale_jobs.apply().get()

    assert read_job(job_id)["status"] == JobStatus.PROCESSING.value
