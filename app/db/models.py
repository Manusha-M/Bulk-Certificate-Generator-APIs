"""ORM models: one GenerationJob has many Certificate rows (one per submitted recipient)."""

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.utils.time import utc_now

# Column lengths. The request schemas use the same limits, so valid input always fits.
CERTIFICATE_TITLE_MAX_LENGTH = 100
COURSE_NAME_MAX_LENGTH = 200
ORGANIZATION_NAME_MAX_LENGTH = 150
RECIPIENT_NAME_MAX_LENGTH = 100
EMAIL_MAX_LENGTH = 320


class JobStatus(enum.StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class CertificateStatus(enum.StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class GenerationJob(Base):
    """One bulk request. Holds the certificate details shared by every recipient."""

    __tablename__ = "generation_jobs"
    __table_args__ = (
        CheckConstraint("total_recipients > 0", name="ck_generation_jobs_total_recipients_positive"),
        CheckConstraint(
            "successful_count >= 0 AND failed_count >= 0",
            name="ck_generation_jobs_counts_non_negative",
        ),
        CheckConstraint(
            "successful_count + failed_count <= total_recipients",
            name="ck_generation_jobs_counts_within_total",
        ),
        Index("ix_generation_jobs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"), default=JobStatus.PENDING
    )

    certificate_title: Mapped[str] = mapped_column(String(CERTIFICATE_TITLE_MAX_LENGTH))
    course_name: Mapped[str] = mapped_column(String(COURSE_NAME_MAX_LENGTH))
    organization_name: Mapped[str] = mapped_column(String(ORGANIZATION_NAME_MAX_LENGTH))
    event_date: Mapped[date] = mapped_column(Date)

    total_recipients: Mapped[int] = mapped_column(Integer)
    successful_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    # Job-level failure reason (individual failures live on the certificate rows).
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    certificates: Mapped[list["Certificate"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Certificate.position",
    )


class Certificate(Base):
    """The generation result for one recipient of a job."""

    __tablename__ = "certificates"
    __table_args__ = (
        # One row per submitted recipient: position is the recipient's index in the request.
        UniqueConstraint("job_id", "position", name="uq_certificates_job_id_position"),
        CheckConstraint("position >= 0", name="ck_certificates_position_non_negative"),
        CheckConstraint(
            "status <> 'COMPLETED' OR file_path IS NOT NULL",
            name="ck_certificates_completed_has_file",
        ),
        CheckConstraint(
            "status <> 'FAILED' OR error_message IS NOT NULL",
            name="ck_certificates_failed_has_error",
        ),
        # Serves "certificates of job X" and "certificates of job X with status Y".
        Index("ix_certificates_job_id_status", "job_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "generation_jobs.id",
            ondelete="CASCADE",
            name="fk_certificates_job_id_generation_jobs",
        )
    )
    position: Mapped[int] = mapped_column(Integer)
    recipient_name: Mapped[str] = mapped_column(String(RECIPIENT_NAME_MAX_LENGTH))
    recipient_email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH))

    status: Mapped[CertificateStatus] = mapped_column(
        Enum(CertificateStatus, name="certificate_status"), default=CertificateStatus.PENDING
    )
    # Path relative to OUTPUT_DIR ("<job_id>/<certificate_id>.pdf"). Never sent to clients.
    file_path: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[GenerationJob] = relationship(back_populates="certificates")
