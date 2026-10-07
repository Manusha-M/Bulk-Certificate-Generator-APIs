"""Creating a generation job: POST /api/v1/jobs."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Certificate, CertificateStatus, GenerationJob, JobStatus
from app.schemas.job import JobCreate
from app.services import job_service
from tests.conftest import make_job_payload, make_recipients


def test_create_job_returns_202_with_job_id(client: TestClient) -> None:
    response = client.post("/api/v1/jobs", json=make_job_payload())

    assert response.status_code == 202
    body = response.json()
    job_id = uuid.UUID(body["job_id"])
    assert body["status"] == "PENDING"
    assert body["total_recipients"] == 2
    assert body["status_url"] == f"/api/v1/jobs/{job_id}"
    assert body["certificates_url"] == f"/api/v1/jobs/{job_id}/certificates"
    assert response.headers["Location"] == body["status_url"]


def test_create_job_stores_job_and_one_record_per_recipient(
    client: TestClient, db_session: Session
) -> None:
    payload = make_job_payload(recipients=make_recipients("Ann Lee", "Bob Ray", "Cy Twombly"))

    job_id = uuid.UUID(client.post("/api/v1/jobs", json=payload).json()["job_id"])

    job = db_session.get(GenerationJob, job_id)
    assert job is not None
    assert job.course_name == "Advanced Python Backend Development"
    assert job.total_recipients == 3
    assert [c.recipient_name for c in job.certificates] == ["Ann Lee", "Bob Ray", "Cy Twombly"]
    assert [c.position for c in job.certificates] == [0, 1, 2]


def test_new_job_and_certificates_start_pending(db_session: Session) -> None:
    """Checked at the service level: through the API the background task runs immediately."""
    job = job_service.create_job(db_session, JobCreate.model_validate(make_job_payload()))

    db_session.expire_all()
    stored = db_session.get(GenerationJob, job.id)
    assert stored is not None
    assert stored.status == JobStatus.PENDING
    assert (stored.successful_count, stored.failed_count) == (0, 0)
    assert stored.started_at is None and stored.completed_at is None
    assert all(c.status == CertificateStatus.PENDING for c in stored.certificates)
    assert all(c.file_path is None for c in stored.certificates)


def test_created_job_is_processed_in_background(client: TestClient, db_session: Session) -> None:
    job_id = client.post("/api/v1/jobs", json=make_job_payload()).json()["job_id"]

    job = db_session.get(GenerationJob, uuid.UUID(job_id))
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    assert job.successful_count == 2


def test_create_job_normalizes_input(client: TestClient, db_session: Session) -> None:
    payload = make_job_payload(
        course_name="  Advanced   Python  ",
        recipients=[{"name": "  John \t  Doe ", "email": " John.Doe@Example.COM "}],
    )

    job_id = uuid.UUID(client.post("/api/v1/jobs", json=payload).json()["job_id"])

    certificate = db_session.scalars(select(Certificate).where(Certificate.job_id == job_id)).one()
    assert certificate.recipient_name == "John Doe"
    assert certificate.recipient_email == "john.doe@example.com"
    assert certificate.job.course_name == "Advanced Python"


def test_bulk_job_with_many_recipients(client: TestClient) -> None:
    names = [f"Recipient Number {index}" for index in range(25)]

    body = client.post("/api/v1/jobs", json=make_job_payload(recipients=make_recipients(*names)))

    job_id = body.json()["job_id"]
    status = client.get(f"/api/v1/jobs/{job_id}").json()
    assert status["status"] == "COMPLETED"
    assert status["completed"] == 25
    assert status["progress_percentage"] == 100.0
