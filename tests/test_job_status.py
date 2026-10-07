"""Job status and progress: GET /api/v1/jobs/{job_id}, plus status transitions."""

import threading
import time
import uuid
from functools import partial
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import GenerationJob, JobStatus
from app.schemas.job import JobCreate
from app.services import generation_service, job_service
from app.services.certificate_generator import CertificateData, CertificateGenerator
from app.services.generation_service import final_job_status, process_job
from app.services.job_service import JobProgress
from tests.conftest import TEMPLATE_PATH, make_job_payload, make_recipients, random_uuid


def test_pending_job_status(client: TestClient, db_session: Session) -> None:
    job = job_service.create_job(db_session, JobCreate.model_validate(make_job_payload()))

    body = client.get(f"/api/v1/jobs/{job.id}").json()

    assert body["status"] == "PENDING"
    assert body["is_finished"] is False
    assert (body["total"], body["completed"], body["failed"], body["pending"]) == (2, 0, 0, 2)
    assert body["progress_percentage"] == 0.0
    assert body["started_at"] is None
    assert body["completed_at"] is None


def test_completed_job_status(client: TestClient) -> None:
    job_id = client.post("/api/v1/jobs", json=make_job_payload()).json()["job_id"]

    body = client.get(f"/api/v1/jobs/{job_id}").json()

    assert body["status"] == "COMPLETED"
    assert body["is_finished"] is True
    assert (body["total"], body["completed"], body["failed"], body["pending"]) == (2, 2, 0, 0)
    assert body["progress_percentage"] == 100.0
    assert body["started_at"] is not None
    assert body["completed_at"] is not None
    assert body["error"] is None
    assert body["course_name"] == "Advanced Python Backend Development"


class ProgressProbe(CertificateGenerator):
    """Records the job's state (read through a separate session) before each certificate."""

    def __init__(self, output_dir: Path, session_factory: sessionmaker[Session]) -> None:
        super().__init__(TEMPLATE_PATH, output_dir)
        self.session_factory = session_factory
        self.snapshots: list[tuple[JobStatus, JobProgress]] = []

    def generate_certificate(self, data: CertificateData) -> str:
        with self.session_factory() as session:
            job = job_service.get_job(session, data.job_id)
            self.snapshots.append((job.status, job_service.get_job_progress(job)))
        return super().generate_certificate(data)


def test_processing_state_and_live_progress(
    db_session: Session, session_factory: sessionmaker[Session], output_dir: Path
) -> None:
    payload = make_job_payload(recipients=make_recipients("Ann", "Bob", "Cy", "Dee"))
    job = job_service.create_job(db_session, JobCreate.model_validate(payload))
    probe = ProgressProbe(output_dir, session_factory)

    process_job(job.id, session_factory=session_factory, generator=probe)

    statuses = [status for status, _ in probe.snapshots]
    assert statuses == [JobStatus.PROCESSING] * 4
    assert [(p.completed, p.pending, p.percentage) for _, p in probe.snapshots] == [
        (0, 4, 0.0),
        (1, 3, 25.0),
        (2, 2, 50.0),
        (3, 1, 75.0),
    ]


def test_status_transitions_from_pending_to_completed(
    db_session: Session, session_factory: sessionmaker[Session], generator: CertificateGenerator
) -> None:
    job = job_service.create_job(db_session, JobCreate.model_validate(make_job_payload()))
    assert job.status == JobStatus.PENDING

    process_job(job.id, session_factory=session_factory, generator=generator)

    db_session.expire_all()
    stored = db_session.get(GenerationJob, job.id)
    assert stored is not None
    assert stored.status == JobStatus.COMPLETED
    assert stored.started_at is not None and stored.completed_at is not None
    assert stored.started_at <= stored.completed_at


def test_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/jobs/{random_uuid()}")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Job not found"}}


def test_invalid_job_id_returns_422(client: TestClient) -> None:
    response = client.get("/api/v1/jobs/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "path.job_id"


@pytest.mark.parametrize(
    ("progress", "pending", "percentage"),
    [
        (JobProgress(total=100, completed=65, failed=2), 33, 67.0),
        (JobProgress(total=100, completed=98, failed=2), 0, 100.0),
        (JobProgress(total=3, completed=1, failed=0), 2, 33.3),
        (JobProgress(total=4, completed=0, failed=0), 4, 0.0),
    ],
)
def test_progress_calculation(progress: JobProgress, pending: int, percentage: float) -> None:
    assert progress.pending == pending
    assert progress.percentage == percentage


@pytest.mark.parametrize(
    ("successful", "failed", "expected"),
    [
        (3, 0, JobStatus.COMPLETED),
        (2, 1, JobStatus.COMPLETED_WITH_ERRORS),
        (0, 3, JobStatus.FAILED),
    ],
)
def test_final_job_status(successful: int, failed: int, expected: JobStatus) -> None:
    assert final_job_status(successful, failed) == expected


class CountingGenerator(CertificateGenerator):
    """Counts calls and tracks how many certificates are being rendered at the same time."""

    def __init__(self, output_dir: Path, delay: float = 0.0) -> None:
        super().__init__(TEMPLATE_PATH, output_dir)
        self.delay = delay
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def generate_certificate(self, data: CertificateData) -> str:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            return super().generate_certificate(data)
        finally:
            with self._lock:
                self.active -= 1


def test_job_can_only_be_claimed_once(
    db_session: Session, session_factory: sessionmaker[Session]
) -> None:
    job = job_service.create_job(db_session, JobCreate.model_validate(make_job_payload()))

    assert generation_service._claim_job(session_factory, job.id) is True
    assert generation_service._claim_job(session_factory, job.id) is False


def test_job_claimed_by_another_worker_is_not_processed_again(
    db_session: Session, session_factory: sessionmaker[Session], output_dir: Path
) -> None:
    job = job_service.create_job(db_session, JobCreate.model_validate(make_job_payload()))
    job.status = JobStatus.PROCESSING  # as if another worker had already claimed it
    db_session.commit()
    counter = CountingGenerator(output_dir)

    process_job(job.id, session_factory=session_factory, generator=counter)

    db_session.expire_all()
    stored = db_session.get(GenerationJob, job.id)
    assert stored is not None
    assert counter.calls == 0
    assert stored.status == JobStatus.PROCESSING
    assert stored.started_at is None


def test_processing_a_finished_job_again_changes_nothing(
    db_session: Session, session_factory: sessionmaker[Session], output_dir: Path
) -> None:
    job = job_service.create_job(db_session, JobCreate.model_validate(make_job_payload()))
    counter = CountingGenerator(output_dir)
    process_job(job.id, session_factory=session_factory, generator=counter)
    db_session.expire_all()
    first_completed_at = db_session.get(GenerationJob, job.id).completed_at  # type: ignore[union-attr]

    process_job(job.id, session_factory=session_factory, generator=counter)

    db_session.expire_all()
    stored = db_session.get(GenerationJob, job.id)
    assert stored is not None
    assert counter.calls == 2  # once per recipient, from the first run only
    assert stored.completed_at == first_completed_at
    assert (stored.successful_count, stored.failed_count) == (2, 0)


def test_processing_an_unknown_job_is_a_no_op(
    db_session: Session, session_factory: sessionmaker[Session], output_dir: Path
) -> None:
    counter = CountingGenerator(output_dir)

    process_job(uuid.uuid4(), session_factory=session_factory, generator=counter)

    assert counter.calls == 0
    assert db_session.scalar(select(func.count()).select_from(GenerationJob)) == 0
    assert not output_dir.exists() or not any(output_dir.iterdir())


def test_background_jobs_respect_the_concurrency_limit(
    db_session: Session, session_factory: sessionmaker[Session], output_dir: Path
) -> None:
    jobs = [
        job_service.create_job(db_session, JobCreate.model_validate(make_job_payload()))
        for _ in range(3)
    ]
    counter = CountingGenerator(output_dir, delay=0.1)

    async def run_all() -> None:
        limiter = anyio.CapacityLimiter(1)
        async with anyio.create_task_group() as task_group:
            for job in jobs:
                task_group.start_soon(
                    partial(
                        generation_service.run_job_in_background,
                        job.id,
                        session_factory=session_factory,
                        generator=counter,
                        limiter=limiter,
                    )
                )

    anyio.run(run_all)

    assert counter.calls == 6
    assert counter.max_active == 1  # without the limiter the three jobs would overlap
    db_session.expire_all()
    assert all(db_session.get(GenerationJob, j.id).status == JobStatus.COMPLETED for j in jobs)  # type: ignore[union-attr]
