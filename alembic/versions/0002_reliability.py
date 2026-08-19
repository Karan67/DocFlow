"""phase 2: retries, dead-letter, idempotency

Revision ID: 0002
Revises: 0001
Create Date: Phase 2

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_OLD_STATUSES = "'PENDING', 'PROCESSING', 'DONE', 'FAILED'"
_NEW_STATUSES = (
    "'PENDING', 'PROCESSING', 'RETRYING', 'DONE', 'FAILED', 'DEAD_LETTER'"
)


def upgrade() -> None:
    # Widening a CHECK constraint is a drop + recreate. This is exactly why
    # status is TEXT + CHECK rather than a native enum - ALTER TYPE ADD VALUE
    # cannot run inside a transactional migration.
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_status", "jobs", f"status IN ({_NEW_STATUSES})")

    op.add_column("jobs", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.add_column(
        "jobs", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True)
    )

    # Partial unique index: identical content may exist only once among jobs
    # that have not permanently failed. Re-uploading a file whose job failed is
    # therefore allowed to create a fresh job, while re-uploading one that is
    # queued, running or done returns the existing job.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_jobs_idempotency_key_active
        ON jobs (idempotency_key)
        WHERE idempotency_key IS NOT NULL
          AND status NOT IN ('FAILED', 'DEAD_LETTER')
        """
    )

    # Supports the reaper's sweep for jobs orphaned by a dead worker.
    op.execute(
        "CREATE INDEX ix_jobs_stale_sweep ON jobs (started_at) "
        "WHERE status = 'PROCESSING'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_jobs_stale_sweep")
    op.execute("DROP INDEX IF EXISTS uq_jobs_idempotency_key_active")
    op.drop_column("jobs", "next_retry_at")
    op.drop_column("jobs", "idempotency_key")

    # Any row in a Phase 2-only status must be collapsed before the narrower
    # constraint can be re-applied.
    op.execute("UPDATE jobs SET status = 'PENDING' WHERE status = 'RETRYING'")
    op.execute("UPDATE jobs SET status = 'FAILED' WHERE status = 'DEAD_LETTER'")
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint("ck_jobs_status", "jobs", f"status IN ({_OLD_STATUSES})")
