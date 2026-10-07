import uuid

from fastapi import APIRouter, BackgroundTasks, Request, Response

from app.api.dependencies import DbSession, GeneratorDep, JobLimiterDep, SessionFactoryDep
from app.db.models import Certificate, CertificateStatus, GenerationJob
from app.schemas.certificate import CertificateResult, JobCertificatesResponse
from app.schemas.error import ErrorResponse
from app.schemas.job import JobCreate, JobCreatedResponse, JobStatusResponse
from app.services import job_service
from app.services.generation_service import run_job_in_background

router = APIRouter(prefix="/jobs", tags=["Jobs"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "Job not found"}}
INVALID = {422: {"model": ErrorResponse, "description": "Invalid request"}}


@router.post(
    "",
    status_code=202,
    response_model=JobCreatedResponse,
    responses=INVALID,
    summary="Create a bulk certificate generation job",
    description="Validates the request, stores the job and one record per recipient, and "
    "returns immediately. Certificates are generated in the background; poll `status_url`.",
)
def create_job(
    payload: JobCreate,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    session: DbSession,
    session_factory: SessionFactoryDep,
    generator: GeneratorDep,
    job_limiter: JobLimiterDep,
) -> JobCreatedResponse:
    job = job_service.create_job(session, payload)
    # Scheduled only after the commit above, so the background task always finds the job.
    background_tasks.add_task(
        run_job_in_background,
        job.id,
        session_factory=session_factory,
        generator=generator,
        limiter=job_limiter,
    )
    status_url = _job_url(request, job)
    response.headers["Location"] = status_url
    return JobCreatedResponse(
        job_id=job.id,
        status=job.status,
        total_recipients=job.total_recipients,
        status_url=status_url,
        certificates_url=_certificates_url(request, job),
    )


@router.get(
    "/{job_id}",
    name="get_job_status",
    response_model=JobStatusResponse,
    responses={**NOT_FOUND, **INVALID},
    summary="Get job status and progress",
)
def get_job_status(job_id: uuid.UUID, request: Request, session: DbSession) -> JobStatusResponse:
    job = job_service.get_job(session, job_id)
    progress = job_service.get_job_progress(job)
    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        is_finished=job_service.is_finished(job),
        total=progress.total,
        completed=progress.completed,
        failed=progress.failed,
        pending=progress.pending,
        progress_percentage=progress.percentage,
        certificate_title=job.certificate_title,
        course_name=job.course_name,
        organization_name=job.organization_name,
        event_date=job.event_date,
        error=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        certificates_url=_certificates_url(request, job),
    )


@router.get(
    "/{job_id}/certificates",
    name="list_job_certificates",
    response_model=JobCertificatesResponse,
    responses={**NOT_FOUND, **INVALID},
    summary="List the per-recipient results of a job",
    description="Use `status=FAILED` to see only the recipients whose certificate failed.",
)
def list_job_certificates(
    job_id: uuid.UUID,
    request: Request,
    session: DbSession,
    status: CertificateStatus | None = None,
) -> JobCertificatesResponse:
    job, certificates = job_service.list_job_certificates(session, job_id, status)
    return JobCertificatesResponse(
        job_id=job.id,
        job_status=job.status,
        count=len(certificates),
        certificates=[_certificate_result(request, certificate) for certificate in certificates],
    )


def _job_url(request: Request, job: GenerationJob) -> str:
    return request.app.url_path_for("get_job_status", job_id=str(job.id))


def _certificates_url(request: Request, job: GenerationJob) -> str:
    return request.app.url_path_for("list_job_certificates", job_id=str(job.id))


def _certificate_result(request: Request, certificate: Certificate) -> CertificateResult:
    completed = certificate.status == CertificateStatus.COMPLETED
    failed = certificate.status == CertificateStatus.FAILED
    return CertificateResult(
        id=certificate.id,
        recipient_index=certificate.position,
        recipient_name=certificate.recipient_name,
        recipient_email=certificate.recipient_email,
        status=certificate.status,
        download_url=(
            request.app.url_path_for("download_certificate", certificate_id=str(certificate.id))
            if completed
            else None
        ),
        error=certificate.error_message if failed else None,
        completed_at=certificate.completed_at,
    )
