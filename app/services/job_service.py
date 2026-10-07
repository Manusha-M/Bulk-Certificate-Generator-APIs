"""Creating jobs and reading their state. Called by the API layer."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.db.models import Certificate, CertificateStatus, GenerationJob, JobStatus
from app.schemas.job import JobCreate

logger = logging.getLogger(__name__)

FINAL_JOB_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_ERRORS, JobStatus.FAILED}
)


@dataclass(frozen=True)
class JobProgress:
    total: int
    completed: int
    failed: int

    @property
    def pending(self) -> int:
        return self.total - self.completed - self.failed

    @property
    def percentage(self) -> float:
        if self.total == 0:
            return 0.0
        return round((self.completed + self.failed) / self.total * 100, 1)


def create_job(session: Session, request: JobCreate) -> GenerationJob:
    """Store the job and one PENDING certificate row per recipient, in one transaction."""
    job = GenerationJob(
        status=JobStatus.PENDING,
        certificate_title=request.certificate_title,
        course_name=request.course_name,
        organization_name=request.organization_name,
        event_date=request.event_date,
        total_recipients=len(request.recipients),
        successful_count=0,
        failed_count=0,
        certificates=[
            Certificate(
                position=index,
                recipient_name=recipient.name,
                recipient_email=recipient.email,
                status=CertificateStatus.PENDING,
            )
            for index, recipient in enumerate(request.recipients)
        ],
    )
    session.add(job)
    session.commit()
    logger.info("Job created job_id=%s total_recipients=%d", job.id, job.total_recipients)
    return job


def get_job(session: Session, job_id: uuid.UUID) -> GenerationJob:
    job = session.get(GenerationJob, job_id)
    if job is None:
        raise NotFoundError("Job not found")
    return job


def list_job_certificates(
    session: Session, job_id: uuid.UUID, status: CertificateStatus | None = None
) -> tuple[GenerationJob, Sequence[Certificate]]:
    job = get_job(session, job_id)
    query = select(Certificate).where(Certificate.job_id == job_id).order_by(Certificate.position)
    if status is not None:
        query = query.where(Certificate.status == status)
    return job, session.scalars(query).all()


def get_job_progress(job: GenerationJob) -> JobProgress:
    return JobProgress(
        total=job.total_recipients, completed=job.successful_count, failed=job.failed_count
    )


def is_finished(job: GenerationJob) -> bool:
    return job.status in FINAL_JOB_STATUSES
