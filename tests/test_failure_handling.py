"""One recipient failing must not stop the others, and every failure must be explained."""

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Certificate, CertificateStatus, GenerationJob, JobStatus
from app.schemas.job import JobCreate
from app.services import generation_service, job_service
from app.services.certificate_generator import CertificateGenerator
from tests.conftest import FailingGenerator, make_job_payload, make_recipients

UseGenerator = Callable[[CertificateGenerator], None]


def run_job(client: TestClient, *names: str) -> tuple[dict, dict]:
    payload = make_job_payload(recipients=make_recipients(*names))
    job_id = client.post("/api/v1/jobs", json=payload).json()["job_id"]
    status = client.get(f"/api/v1/jobs/{job_id}").json()
    results = client.get(f"/api/v1/jobs/{job_id}/certificates").json()
    return status, results


def test_one_failure_does_not_stop_other_recipients(
    client: TestClient, use_generator: UseGenerator, output_dir: Path
) -> None:
    use_generator(FailingGenerator(output_dir, fail_for={"Recipient B"}))

    status, results = run_job(client, "Recipient A", "Recipient B", "Recipient C")

    by_name = {c["recipient_name"]: c for c in results["certificates"]}
    assert [by_name[n]["status"] for n in ("Recipient A", "Recipient B", "Recipient C")] == [
        "COMPLETED",
        "FAILED",
        "COMPLETED",
    ]
    assert by_name["Recipient B"]["error"] == "Simulated generation failure"
    assert by_name["Recipient B"]["download_url"] is None
    assert by_name["Recipient A"]["download_url"] == f"/api/v1/certificates/{by_name['Recipient A']['id']}"
    assert by_name["Recipient A"]["error"] is None

    assert status["status"] == "COMPLETED_WITH_ERRORS"
    assert (status["completed"], status["failed"], status["pending"]) == (2, 1, 0)
    assert status["progress_percentage"] == 100.0
    assert status["is_finished"] is True
    assert status["error"] is None
    assert len(list(output_dir.rglob("*.pdf"))) == 2


def test_failed_recipients_can_be_listed_on_their_own(
    client: TestClient, use_generator: UseGenerator, output_dir: Path
) -> None:
    use_generator(FailingGenerator(output_dir, fail_for={"Bob", "Dee"}))
    payload = make_job_payload(recipients=make_recipients("Ann", "Bob", "Cy", "Dee"))
    job_id = client.post("/api/v1/jobs", json=payload).json()["job_id"]

    failed = client.get(f"/api/v1/jobs/{job_id}/certificates", params={"status": "FAILED"}).json()

    assert failed["count"] == 2
    assert [c["recipient_name"] for c in failed["certificates"]] == ["Bob", "Dee"]
    assert [c["recipient_index"] for c in failed["certificates"]] == [1, 3]


def test_real_generation_failure_without_mocks(client: TestClient) -> None:
    """A name the certificate font cannot draw fails only that certificate."""
    status, results = run_job(client, "Ann Lee", "李雷", "Cy Young")

    assert [c["status"] for c in results["certificates"]] == ["COMPLETED", "FAILED", "COMPLETED"]
    assert "cannot display" in results["certificates"][1]["error"]
    assert status["status"] == "COMPLETED_WITH_ERRORS"


def test_all_certificates_failing_marks_job_failed(
    client: TestClient, use_generator: UseGenerator, output_dir: Path
) -> None:
    use_generator(FailingGenerator(output_dir, fail_for={"Ann", "Bob"}))

    status, results = run_job(client, "Ann", "Bob")

    assert status["status"] == "FAILED"
    assert status["error"] == generation_service.ALL_FAILED_ERROR
    assert (status["completed"], status["failed"]) == (0, 2)
    assert all(c["error"] for c in results["certificates"])


@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        (OSError(28, "No space left on device: '/srv/secret/path.pdf'"), generation_service.FILE_WRITE_ERROR),
        (RuntimeError("internal detail: pointer 0xdeadbeef"), generation_service.UNEXPECTED_ERROR),
    ],
    ids=["filesystem-error", "unexpected-error"],
)  # fmt: skip
def test_internal_errors_are_recorded_without_leaking_details(
    client: TestClient,
    use_generator: UseGenerator,
    output_dir: Path,
    error: Exception,
    expected_message: str,
) -> None:
    use_generator(FailingGenerator(output_dir, fail_for={"Bob"}, error=error))

    status, results = run_job(client, "Ann", "Bob")

    failed = results["certificates"][1]
    assert failed["status"] == "FAILED"
    assert failed["error"] == expected_message
    assert "secret" not in str(results) and "0xdeadbeef" not in str(results)
    assert status["status"] == "COMPLETED_WITH_ERRORS"


def test_failure_reason_is_stored_in_database(
    client: TestClient, use_generator: UseGenerator, output_dir: Path, db_session: Session
) -> None:
    use_generator(FailingGenerator(output_dir, fail_for={"Bob"}))
    run_job(client, "Ann", "Bob")

    failed = db_session.query(Certificate).filter_by(recipient_name="Bob").one()
    assert failed.status == CertificateStatus.FAILED
    assert failed.error_message == "Simulated generation failure"
    assert failed.file_path is None
    assert failed.completed_at is not None


def test_database_error_while_saving_a_result_fails_only_that_certificate(
    db_session: Session,
    session_factory: sessionmaker[Session],
    generator: CertificateGenerator,
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = make_job_payload(recipients=make_recipients("Ann", "Bob", "Cy"))
    job = job_service.create_job(db_session, JobCreate.model_validate(payload))
    bob_id = job.certificates[1].id
    original_save = generation_service._save_result

    def flaky_save(session: Session, certificate_id, status, **kwargs) -> None:  # type: ignore[no-untyped-def]
        if certificate_id == bob_id and status == CertificateStatus.COMPLETED:
            raise OperationalError("UPDATE certificates ...", {}, Exception("connection reset"))
        original_save(session, certificate_id, status, **kwargs)

    monkeypatch.setattr(generation_service, "_save_result", flaky_save)

    generation_service.process_job(job.id, session_factory=session_factory, generator=generator)

    db_session.expire_all()
    stored = db_session.get(GenerationJob, job.id)
    assert stored is not None
    assert [c.status for c in stored.certificates] == [
        CertificateStatus.COMPLETED,
        CertificateStatus.FAILED,
        CertificateStatus.COMPLETED,
    ]
    assert stored.certificates[1].error_message == generation_service.SAVE_RESULT_ERROR
    assert stored.status == JobStatus.COMPLETED_WITH_ERRORS
    # The orphaned PDF for Bob was removed, so files and records stay in sync.
    assert not (output_dir / str(job.id) / f"{bob_id}.pdf").exists()
    assert len(list(output_dir.rglob("*.pdf"))) == 2


def test_unexpected_job_level_error_marks_job_failed(
    db_session: Session,
    session_factory: sessionmaker[Session],
    generator: CertificateGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = job_service.create_job(db_session, JobCreate.model_validate(make_job_payload()))

    def broken_listing(*args: object) -> list:
        raise OperationalError("SELECT ...", {}, Exception("server closed the connection"))

    monkeypatch.setattr(generation_service, "_pending_certificate_ids", broken_listing)

    generation_service.process_job(job.id, session_factory=session_factory, generator=generator)

    db_session.expire_all()
    stored = db_session.get(GenerationJob, job.id)
    assert stored is not None
    assert stored.status == JobStatus.FAILED
    assert stored.error_message == generation_service.JOB_ABORTED_ERROR
    assert stored.completed_at is not None
    assert all(c.status == CertificateStatus.FAILED for c in stored.certificates)
    assert stored.failed_count == 2
