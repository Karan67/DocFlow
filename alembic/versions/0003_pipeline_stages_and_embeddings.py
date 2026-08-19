"""phase 3: pipeline stages, OCR routing and embeddings

Revision ID: 0003
Revises: 0002
Create Date: Phase 3

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 384


def upgrade() -> None:
    # Provided by the pgvector/pgvector image the compose file now uses.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column(
        "jobs",
        sa.Column(
            "stage",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'extract_text'"),
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "stages",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_jobs_stage", "jobs", "stage IN ('extract_text', 'ocr', 'embed')"
    )

    # A job is now the whole ingestion pipeline rather than a single step, so
    # the old per-step job_type value no longer describes it.
    op.execute("UPDATE jobs SET job_type = 'document' WHERE job_type = 'extract_text'")

    op.create_table(
        "document_chunks",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("job_id", "chunk_index", name="uq_document_chunks_job_index"),
    )
    op.create_index("ix_document_chunks_job_id", "document_chunks", ["job_id"])

    # HNSW for cosine similarity search. Built on an empty table here, which is
    # the cheap moment to do it.
    op.execute(
        """
        CREATE INDEX ix_document_chunks_embedding
        ON document_chunks USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding")
    op.drop_index("ix_document_chunks_job_id", table_name="document_chunks")
    op.drop_table("document_chunks")

    op.execute("UPDATE jobs SET job_type = 'extract_text' WHERE job_type = 'document'")
    op.drop_constraint("ck_jobs_stage", "jobs", type_="check")
    op.drop_column("jobs", "stages")
    op.drop_column("jobs", "stage")
    # The extension is left in place: other objects may depend on it, and
    # dropping it is not this migration's business.
