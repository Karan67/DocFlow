"""System stats for the dashboard.

Queue depth is the headline number for a queue system - it is the one metric
that tells you whether the workers are keeping up. The browser cannot read
Redis, so the API exposes it here.

Deliberately cheap: four Redis LLENs and two grouped counts. The dashboard
polls this every few seconds, so it must not do per-job work.
"""

from __future__ import annotations

import logging

import redis
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.schemas import QueueDepth, StatsResponse
from core.config import settings
from core.database import get_db
from core.models import TERMINAL_STATUSES, DocumentChunk, Job, JobStage, JobStatus
from worker.celery_app import ALL_QUEUES, QUEUE_OCR

logger = logging.getLogger(__name__)

router = APIRouter(tags=["meta"])


def _queue_depths() -> tuple[list[QueueDepth], bool]:
    """Pending message count per queue.

    Celery on Redis stores each queue as a list keyed by the queue name, so
    depth is a plain LLEN. This counts *waiting* messages only - work already
    handed to a worker has been popped, which is why a busy system can show
    zero depth while still being saturated.
    """
    client = None
    try:
        client = redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        pipeline = client.pipeline()
        for name in ALL_QUEUES:
            pipeline.llen(name)
        depths = pipeline.execute()
        return (
            [
                QueueDepth(
                    name=name,
                    depth=int(depth),
                    is_priority_queue=name != QUEUE_OCR,
                )
                for name, depth in zip(ALL_QUEUES, depths)
            ],
            True,
        )
    except Exception as exc:
        logger.warning("Could not read queue depths: %s", exc)
        return (
            [
                QueueDepth(name=name, depth=None, is_priority_queue=name != QUEUE_OCR)
                for name in ALL_QUEUES
            ],
            False,
        )
    finally:
        if client is not None:
            client.close()


@router.get("/stats", response_model=StatsResponse, summary="Queue and job stats")
def get_stats(db: Session = Depends(get_db)) -> StatsResponse:
    queues, broker_reachable = _queue_depths()

    # Zero-fill both breakdowns so the dashboard's layout is stable rather than
    # reflowing every time a status count drops to nothing.
    by_status = {member.value: 0 for member in JobStatus}
    for status_value, count in db.execute(
        select(Job.status, func.count()).group_by(Job.status)
    ).all():
        by_status[status_value] = count

    terminal = [status.value for status in TERMINAL_STATUSES]
    by_stage = {member.value: 0 for member in JobStage}
    for stage_value, count in db.execute(
        select(Job.stage, func.count())
        .where(Job.status.notin_(terminal))
        .group_by(Job.stage)
    ).all():
        by_stage[stage_value] = count

    return StatsResponse(
        queues=queues,
        jobs_by_status=by_status,
        active_by_stage=by_stage,
        total_jobs=sum(by_status.values()),
        total_chunks=db.scalar(select(func.count()).select_from(DocumentChunk)) or 0,
        broker_reachable=broker_reachable,
    )
