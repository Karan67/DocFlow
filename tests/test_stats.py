"""Phase 5: the stats endpoint behind the dashboard.

Run inside the worker container:  docker compose exec worker pytest

The dashboard polls this every two seconds, so two things matter beyond
correctness: it must stay cheap, and its shape must be stable. Zero-filling
both breakdowns is what stops the UI reflowing every time a count drops to
nothing.
"""

from __future__ import annotations

import uuid

import pytest

import api.stats as stats_module
from core.models import JobStage, JobStatus
from worker.celery_app import ALL_QUEUES, QUEUE_OCR

pytestmark = pytest.mark.integration


def test_reports_every_queue_in_topology_order(client):
    body = client.get("/stats").json()

    assert [queue["name"] for queue in body["queues"]] == list(ALL_QUEUES)
    assert all(queue["depth"] >= 0 for queue in body["queues"])
    assert body["broker_reachable"] is True


def test_ocr_queue_is_flagged_as_a_dedicated_pool(client):
    queues = {queue["name"]: queue for queue in client.get("/stats").json()["queues"]}

    assert queues[QUEUE_OCR]["is_priority_queue"] is False
    assert all(
        queues[name]["is_priority_queue"] for name in ALL_QUEUES if name != QUEUE_OCR
    )


def test_status_breakdown_is_zero_filled(client):
    """A polling UI should not reflow as statuses appear and disappear."""
    body = client.get("/stats").json()

    assert set(body["jobs_by_status"]) == {member.value for member in JobStatus}
    assert set(body["active_by_stage"]) == {member.value for member in JobStage}
    assert all(count >= 0 for count in body["jobs_by_status"].values())


def test_totals_agree_with_the_status_breakdown(client):
    body = client.get("/stats").json()
    assert body["total_jobs"] == sum(body["jobs_by_status"].values())


def test_new_job_moves_the_counters(client, upload, unique_pdf):
    before = client.get("/stats").json()

    upload(unique_pdf("stats"))

    after = client.get("/stats").json()
    assert after["total_jobs"] == before["total_jobs"] + 1
    assert after["jobs_by_status"]["PENDING"] == before["jobs_by_status"]["PENDING"] + 1
    # A PENDING job is unfinished, so it counts toward its stage.
    assert (
        after["active_by_stage"]["extract_text"]
        == before["active_by_stage"]["extract_text"] + 1
    )


def test_finished_jobs_leave_the_active_stage_breakdown(
    client, upload, run_pipeline, fake_embedder, unique_pdf
):
    job_id = uuid.UUID(upload(unique_pdf("done")).json()["job_id"])
    before = client.get("/stats").json()

    run_pipeline(job_id)

    after = client.get("/stats").json()
    assert after["jobs_by_status"]["DONE"] == before["jobs_by_status"]["DONE"] + 1
    # It finished on `embed`, but a terminal job is not "active" anywhere.
    assert after["active_by_stage"]["embed"] == before["active_by_stage"]["embed"]
    assert (
        after["active_by_stage"]["extract_text"]
        == before["active_by_stage"]["extract_text"] - 1
    )


def test_survives_an_unreachable_broker(client, monkeypatch):
    """Redis being down must degrade the dashboard, not break it.

    Job status lives in Postgres, so everything except queue depth is still
    answerable - and the endpoint says so rather than returning a 500.
    """

    def exploding_from_url(*args, **kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(stats_module.redis, "from_url", exploding_from_url)

    response = client.get("/stats")

    assert response.status_code == 200
    body = response.json()
    assert body["broker_reachable"] is False
    assert [queue["depth"] for queue in body["queues"]] == [None] * len(ALL_QUEUES)
    # The database-backed half is unaffected.
    assert body["total_jobs"] >= 0
    assert set(body["jobs_by_status"]) == {member.value for member in JobStatus}
