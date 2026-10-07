"""Initial schema: generation_jobs and certificates.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED")
CERTIFICATE_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED")


def upgrade() -> None:
    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Enum(*JOB_STATUSES, name="job_status"), nullable=False),
        sa.Column("certificate_title", sa.String(length=100), nullable=False),
        sa.Column("course_name", sa.String(length=200), nullable=False),
        sa.Column("organization_name", sa.String(length=150), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("total_recipients", sa.Integer(), nullable=False),
        sa.Column("successful_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "total_recipients > 0", name="ck_generation_jobs_total_recipients_positive"
        ),
        sa.CheckConstraint(
            "successful_count >= 0 AND failed_count >= 0",
            name="ck_generation_jobs_counts_non_negative",
        ),
        sa.CheckConstraint(
            "successful_count + failed_count <= total_recipients",
            name="ck_generation_jobs_counts_within_total",
        ),
    )
    op.create_index("ix_generation_jobs_status", "generation_jobs", ["status"])

    op.create_table(
        "certificates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("recipient_name", sa.String(length=100), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        sa.Column(
            "status", sa.Enum(*CERTIFICATE_STATUSES, name="certificate_status"), nullable=False
        ),
        sa.Column("file_path", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["generation_jobs.id"],
            name="fk_certificates_job_id_generation_jobs",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("job_id", "position", name="uq_certificates_job_id_position"),
        sa.CheckConstraint("position >= 0", name="ck_certificates_position_non_negative"),
        sa.CheckConstraint(
            "status <> 'COMPLETED' OR file_path IS NOT NULL",
            name="ck_certificates_completed_has_file",
        ),
        sa.CheckConstraint(
            "status <> 'FAILED' OR error_message IS NOT NULL",
            name="ck_certificates_failed_has_error",
        ),
    )
    op.create_index("ix_certificates_job_id_status", "certificates", ["job_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_certificates_job_id_status", table_name="certificates")
    op.drop_table("certificates")
    op.drop_index("ix_generation_jobs_status", table_name="generation_jobs")
    op.drop_table("generation_jobs")
    sa.Enum(name="certificate_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="job_status").drop(op.get_bind(), checkfirst=True)
