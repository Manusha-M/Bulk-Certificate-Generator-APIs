"""Downloading certificates: GET /api/v1/certificates/{certificate_id}, and the results list."""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy.orm import Session

from app.db.models import Certificate
from app.schemas.job import JobCreate
from app.services import job_service
from app.services.certificate_generator import CertificateGenerator
from tests.conftest import FailingGenerator, make_job_payload, make_recipients, random_uuid

UseGenerator = Callable[[CertificateGenerator], None]


def job_results(client: TestClient, *names: str) -> list[dict]:
    payload = make_job_payload(recipients=make_recipients(*names))
    job_id = client.post("/api/v1/jobs", json=payload).json()["job_id"]
    return client.get(f"/api/v1/jobs/{job_id}/certificates").json()["certificates"]


def test_download_completed_certificate_returns_pdf(client: TestClient, tmp_path: Path) -> None:
    certificate = job_results(client, "Jane Smith")[0]

    response = client.get(certificate["download_url"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="certificate-{certificate["id"]}.pdf"'
    )
    assert response.content.startswith(b"%PDF-")
    downloaded = tmp_path / "downloaded.pdf"
    downloaded.write_bytes(response.content)
    assert "Jane Smith" in PdfReader(downloaded).pages[0].extract_text()


def test_unknown_certificate_returns_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/certificates/{random_uuid()}")

    assert response.status_code == 404
    assert response.json()["error"] == {"code": "not_found", "message": "Certificate not found"}


def test_invalid_certificate_id_returns_422(client: TestClient) -> None:
    response = client.get("/api/v1/certificates/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["details"][0]["field"] == "path.certificate_id"


def test_path_traversal_in_url_serves_nothing(client: TestClient) -> None:
    response = client.get("/api/v1/certificates/..%2F..%2Fetc%2Fpasswd")

    # Either the route does not match (404) or the ID is not a UUID (422): never a file.
    assert response.status_code in (404, 422)
    assert response.headers["content-type"] == "application/json"
    assert "root:" not in response.text


def test_failed_certificate_cannot_be_downloaded(
    client: TestClient, use_generator: UseGenerator, output_dir: Path
) -> None:
    use_generator(FailingGenerator(output_dir, fail_for={"Bob"}))
    failed = job_results(client, "Ann", "Bob")[1]

    response = client.get(f"/api/v1/certificates/{failed['id']}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "certificate_generation_failed"


def test_pending_certificate_cannot_be_downloaded_yet(
    client: TestClient, db_session: Session
) -> None:
    job = job_service.create_job(db_session, JobCreate.model_validate(make_job_payload()))

    response = client.get(f"/api/v1/certificates/{job.certificates[0].id}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "certificate_not_ready"


def test_missing_file_returns_500_without_exposing_paths(
    client: TestClient, output_dir: Path
) -> None:
    certificate = job_results(client, "Ann")[0]
    for pdf in output_dir.rglob("*.pdf"):
        pdf.unlink()

    response = client.get(certificate["download_url"])

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "certificate_file_unavailable",
        "message": "Certificate file is currently unavailable",
    }


def test_stored_path_outside_output_dir_is_never_served(
    client: TestClient, db_session: Session, output_dir: Path
) -> None:
    # A real file just outside OUTPUT_DIR: only the containment check can stop it.
    secret = output_dir.parent / "secret.pdf"
    secret.write_bytes(b"%PDF-top-secret")
    certificate_id = uuid.UUID(job_results(client, "Ann")[0]["id"])
    certificate = db_session.get(Certificate, certificate_id)
    assert certificate is not None
    certificate.file_path = "../secret.pdf"
    db_session.commit()

    response = client.get(f"/api/v1/certificates/{certificate_id}")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "certificate_file_unavailable"
    assert b"top-secret" not in response.content


def test_results_list_has_download_urls_only_for_completed(
    client: TestClient, use_generator: UseGenerator, output_dir: Path
) -> None:
    use_generator(FailingGenerator(output_dir, fail_for={"Bob"}))

    results = job_results(client, "Ann", "Bob")

    assert results[0]["download_url"] is not None
    assert results[1]["download_url"] is None
    assert "file_path" not in results[0]
    assert ".pdf" not in json.dumps(results)  # no stored file path leaks into the response


def test_results_for_unknown_job_return_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/jobs/{random_uuid()}/certificates")

    assert response.status_code == 404


def test_results_reject_unknown_status_filter(client: TestClient) -> None:
    job_id = client.post("/api/v1/jobs", json=make_job_payload()).json()["job_id"]

    response = client.get(f"/api/v1/jobs/{job_id}/certificates", params={"status": "DONE"})

    assert response.status_code == 422
