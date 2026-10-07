"""Background processing of a certificate generation job.

`process_job` runs after the API has already answered the client. It never uses the
request's database session; every step opens its own short transaction:

1. Claim the job: PENDING -> PROCESSING with one conditional UPDATE. Only one caller can
   win, so a job is never processed twice.
2. For each certificate, in request order:
   a. mark it PROCESSING (own transaction),
   b. render the PDF (no transaction open while the file is written),
   c. save COMPLETED or FAILED and bump the job counter (one transaction).
   A failing certificate is recorded and the loop moves on; it never rolls back
   certificates that already succeeded.
3. Finalise: set COMPLETED, COMPLETED_WITH_ERRORS or FAILED from the certificate rows.
"""

import logging
import uuid
from functools import partial

import anyio
from anyio import CapacityLimiter
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import InstrumentedAttribute, Session, sessionmaker

from app.db.models import Certificate, CertificateStatus, GenerationJob, JobStatus
from app.services.certificate_generator import (
    CertificateData,
    CertificateGenerationError,
    CertificateGenerator,
)
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

# Client-facing failure reasons. Details (paths, tracebacks) only go to the logs.
FILE_WRITE_ERROR = "Could not write the certificate file"
UNEXPECTED_ERROR = "Unexpected error while generating the certificate"
SAVE_RESULT_ERROR = "Could not save the certificate result"
ALL_FAILED_ERROR = "All certificates in this job failed to generate"
JOB_ABORTED_ERROR = "Job processing stopped unexpectedly"


def final_job_status(successful: int, failed: int) -> JobStatus:
    if failed == 0:
        return JobStatus.COMPLETED
    if successful == 0:
        return JobStatus.FAILED
    return JobStatus.COMPLETED_WITH_ERRORS


async def run_job_in_background(
    job_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
    generator: CertificateGenerator,
    limiter: CapacityLimiter,
) -> None:
    """Entry point for FastAPI's BackgroundTasks.

    Runs `process_job` in a worker thread under its own limiter: at most
    `limiter.total_tokens` jobs generate at the same time, extra jobs wait (still PENDING),
    and running jobs never use up the threads that serve API requests.
    """
    work = partial(process_job, job_id, session_factory=session_factory, generator=generator)
    await anyio.to_thread.run_sync(work, limiter=limiter)


def process_job(
    job_id: uuid.UUID,
    *,
    session_factory: sessionmaker[Session],
    generator: CertificateGenerator,
) -> None:
    """Generate every certificate of a job. Safe to call more than once for the same job."""
    try:
        if not _claim_job(session_factory, job_id):
            logger.warning("Job skipped because it is missing or not pending job_id=%s", job_id)
            return
        logger.info("Job processing started job_id=%s", job_id)

        for certificate_id in _pending_certificate_ids(session_factory, job_id):
            _process_certificate(session_factory, generator, certificate_id)

        status = _finalize_job(session_factory, job_id)
        logger.info("Job completed job_id=%s status=%s", job_id, status)
    except Exception:
        # Last line of defence: nothing above a background task would report this error.
        logger.exception("Job processing stopped unexpectedly job_id=%s", job_id)
        _abort_job(session_factory, job_id)


def _claim_job(session_factory: sessionmaker[Session], job_id: uuid.UUID) -> bool:
    with session_factory.begin() as session:
        result = session.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.PENDING)
            .values(status=JobStatus.PROCESSING, started_at=utc_now())
        )
        return result.rowcount == 1


def _pending_certificate_ids(
    session_factory: sessionmaker[Session], job_id: uuid.UUID
) -> list[uuid.UUID]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(Certificate.id)
                .where(
                    Certificate.job_id == job_id,
                    Certificate.status == CertificateStatus.PENDING,
                )
                .order_by(Certificate.position)
            )
        )


def _process_certificate(
    session_factory: sessionmaker[Session],
    generator: CertificateGenerator,
    certificate_id: uuid.UUID,
) -> None:
    data = _start_certificate(session_factory, certificate_id)
    if data is None:
        return
    logger.info(
        "Certificate generation started certificate_id=%s job_id=%s", certificate_id, data.job_id
    )
    try:
        file_path = generator.generate_certificate(data)
    except CertificateGenerationError as exc:
        logger.warning(
            "Certificate generation failed certificate_id=%s job_id=%s reason=%s",
            certificate_id,
            data.job_id,
            exc,
        )
        _record_failure(session_factory, data, str(exc))
    except OSError:
        logger.exception(
            "Certificate file could not be written certificate_id=%s job_id=%s",
            certificate_id,
            data.job_id,
        )
        _record_failure(session_factory, data, FILE_WRITE_ERROR)
    except Exception:
        logger.exception(
            "Unexpected error while generating certificate certificate_id=%s job_id=%s",
            certificate_id,
            data.job_id,
        )
        _record_failure(session_factory, data, UNEXPECTED_ERROR)
    else:
        _record_success(session_factory, generator, data, file_path)


def _start_certificate(
    session_factory: sessionmaker[Session], certificate_id: uuid.UUID
) -> CertificateData | None:
    """Mark the certificate PROCESSING and return what needs to be printed on it."""
    with session_factory.begin() as session:
        certificate = session.get(Certificate, certificate_id)
        if certificate is None or certificate.status != CertificateStatus.PENDING:
            return None
        certificate.status = CertificateStatus.PROCESSING
        job = certificate.job
        return CertificateData(
            certificate_id=certificate.id,
            job_id=job.id,
            recipient_name=certificate.recipient_name,
            certificate_title=job.certificate_title,
            course_name=job.course_name,
            organization_name=job.organization_name,
            event_date=job.event_date,
        )


def _record_success(
    session_factory: sessionmaker[Session],
    generator: CertificateGenerator,
    data: CertificateData,
    file_path: str,
) -> None:
    try:
        with session_factory.begin() as session:
            _save_result(session, data.certificate_id, CertificateStatus.COMPLETED, file_path=file_path)
            _increment_counter(session, data.job_id, GenerationJob.successful_count)
    except SQLAlchemyError:
        logger.exception(
            "Could not save certificate result certificate_id=%s job_id=%s",
            data.certificate_id,
            data.job_id,
        )
        generator.delete_certificate(file_path)  # do not leave a file without a record
        _record_failure(session_factory, data, SAVE_RESULT_ERROR)
        return
    logger.info(
        "Certificate generated successfully certificate_id=%s job_id=%s",
        data.certificate_id,
        data.job_id,
    )


def _record_failure(
    session_factory: sessionmaker[Session], data: CertificateData, error_message: str
) -> None:
    with session_factory.begin() as session:
        _save_result(session, data.certificate_id, CertificateStatus.FAILED, error_message=error_message)
        _increment_counter(session, data.job_id, GenerationJob.failed_count)


def _save_result(
    session: Session,
    certificate_id: uuid.UUID,
    status: CertificateStatus,
    *,
    file_path: str | None = None,
    error_message: str | None = None,
) -> None:
    session.execute(
        update(Certificate)
        .where(Certificate.id == certificate_id)
        .values(
            status=status,
            file_path=file_path,
            error_message=error_message,
            completed_at=utc_now(),
        )
    )


def _increment_counter(
    session: Session, job_id: uuid.UUID, counter: InstrumentedAttribute[int]
) -> None:
    # Done in SQL (counter = counter + 1) so the database does the arithmetic atomically.
    session.execute(
        update(GenerationJob).where(GenerationJob.id == job_id).values({counter: counter + 1})
    )


def _count_results(session: Session, job_id: uuid.UUID) -> tuple[int, int]:
    rows = session.execute(
        select(Certificate.status, func.count())
        .where(Certificate.job_id == job_id)
        .group_by(Certificate.status)
    ).all()
    counts = {status: count for status, count in rows}
    return counts.get(CertificateStatus.COMPLETED, 0), counts.get(CertificateStatus.FAILED, 0)


def _finalize_job(session_factory: sessionmaker[Session], job_id: uuid.UUID) -> JobStatus:
    with session_factory.begin() as session:
        job = session.get(GenerationJob, job_id, with_for_update=True)
        if job is None:
            raise LookupError(f"Job {job_id} disappeared during processing")
        # Recount from the certificate rows: they are the source of truth for the counters.
        successful, failed = _count_results(session, job_id)
        job.successful_count = successful
        job.failed_count = failed
        job.status = final_job_status(successful, failed)
        job.error_message = ALL_FAILED_ERROR if job.status == JobStatus.FAILED else None
        job.completed_at = utc_now()
        return job.status


def _abort_job(session_factory: sessionmaker[Session], job_id: uuid.UUID) -> None:
    """Mark the job and its unfinished certificates FAILED after an unexpected error."""
    try:
        with session_factory.begin() as session:
            session.execute(
                update(Certificate)
                .where(
                    Certificate.job_id == job_id,
                    Certificate.status.in_([CertificateStatus.PENDING, CertificateStatus.PROCESSING]),
                )
                .values(
                    status=CertificateStatus.FAILED,
                    error_message=JOB_ABORTED_ERROR,
                    completed_at=utc_now(),
                )
            )
            successful, failed = _count_results(session, job_id)
            session.execute(
                update(GenerationJob)
                .where(GenerationJob.id == job_id)
                .values(
                    status=JobStatus.FAILED,
                    successful_count=successful,
                    failed_count=failed,
                    error_message=JOB_ABORTED_ERROR,
                    completed_at=utc_now(),
                )
            )
    except Exception:
        logger.exception("Could not mark job as failed job_id=%s", job_id)
