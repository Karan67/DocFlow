"""phase 4: job priority

Revision ID: 0004
Revises: 0003
Create Date: Phase 4

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "priority", sa.Text(), nullable=False, server_default=sa.text("'normal'")
        ),
    )
    op.create_check_constraint(
        "ck_jobs_priority", "jobs", "priority IN ('high', 'normal', 'low')"
    )
    # Supports the dashboard's "what is waiting, most urgent first" view.
    op.create_index("ix_jobs_priority_status", "jobs", ["priority", "status"])


def downgrade() -> None:
    op.drop_index("ix_jobs_priority_status", table_name="jobs")
    op.drop_constraint("ck_jobs_priority", "jobs", type_="check")
    op.drop_column("jobs", "priority")
