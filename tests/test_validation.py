"""Input validation. Invalid requests are rejected with 422 before anything is stored."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import GenerationJob
from app.schemas.job import MAX_RECIPIENTS_PER_JOB, JobCreate
from app.schemas.recipient import RecipientIn
from tests.conftest import make_job_payload, make_recipients


def post_job(client: TestClient, payload: Any) -> Any:
    # json.dumps escapes non-ASCII as \uXXXX, so even a lone surrogate reaches the API as
    # valid JSON text, exactly as a real client could send it.
    return client.post(
        "/api/v1/jobs", content=json.dumps(payload), headers={"Content-Type": "application/json"}
    )


def error_fields(response: Any) -> list[str]:
    return [detail["field"] for detail in response.json()["error"]["details"]]


@pytest.mark.parametrize(
    ("payload", "expected_field"),
    [
        (make_job_payload(recipients=[]), "body.recipients"),
        ({k: v for k, v in make_job_payload().items() if k != "recipients"}, "body.recipients"),
        (make_job_payload(recipients=[{"email": "a@example.com"}]), "body.recipients.0.name"),
        (make_job_payload(recipients=[{"name": "   ", "email": "a@example.com"}]), "body.recipients.0.name"),
        (make_job_payload(recipients=[{"name": "A" * 101, "email": "a@example.com"}]), "body.recipients.0.name"),
        (make_job_payload(recipients=[{"name": 123, "email": "a@example.com"}]), "body.recipients.0.name"),
        (make_job_payload(recipients=[{"name": "Ann", "email": "not-an-email"}]), "body.recipients.0.email"),
        (make_job_payload(recipients=[{"name": "Ann"}]), "body.recipients.0.email"),
        (make_job_payload(recipients=[{"name": "Ann\x00", "email": "a@example.com"}]), "body.recipients.0.name"),
        (make_job_payload(recipients=[{"name": "Ann", "email": "a@example.com", "x": 1}]), "body.recipients.0.x"),
        (make_job_payload(recipients="John Doe"), "body.recipients"),
        (make_job_payload(course_name=""), "body.course_name"),
        (make_job_payload(course_name="C" * 201), "body.course_name"),
        (make_job_payload(organization_name=None), "body.organization_name"),
        (make_job_payload(certificate_title="ab"), "body.certificate_title"),
        (make_job_payload(event_date="2026-13-45"), "body.event_date"),
        (make_job_payload(event_date="next tuesday"), "body.event_date"),
        (make_job_payload(unexpected_field="value"), "body.unexpected_field"),
        (make_job_payload(recipients=[{"name": "\ud800x", "email": "a@example.com"}]), "body.recipients.0.name"),
        (make_job_payload(organization_name="Politechnika Łódzka"), "body.organization_name"),
        (make_job_payload(course_name="Python → Expert"), "body.course_name"),
        (make_job_payload(certificate_title="Award for µ Labs"), "body.certificate_title"),
    ],
    ids=[
        "empty-recipients", "missing-recipients", "missing-name", "blank-name", "name-too-long",
        "name-not-string", "invalid-email", "missing-email", "control-character", "unknown-recipient-field",
        "recipients-not-a-list", "empty-course", "course-too-long", "null-organization", "title-too-short",
        "impossible-date", "malformed-date", "unknown-job-field", "lone-surrogate",
        "organization-font-unsupported", "course-font-unsupported", "title-uppercase-font-unsupported",
    ],
)  # fmt: skip
def test_invalid_request_is_rejected(
    client: TestClient, db_session: Session, payload: dict[str, Any], expected_field: str
) -> None:
    response = post_job(client, payload)

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert expected_field in error_fields(response)
    assert db_session.scalar(select(func.count()).select_from(GenerationJob)) == 0


def test_validation_errors_point_at_the_bad_recipient(client: TestClient) -> None:
    recipients = make_recipients("Ann", "Bob", "Cy")
    recipients[2]["email"] = "broken@"

    response = post_job(client, make_job_payload(recipients=recipients))

    assert response.status_code == 422
    assert error_fields(response) == ["body.recipients.2.email"]


def test_validation_errors_do_not_echo_input(client: TestClient) -> None:
    response = post_job(client, make_job_payload(recipients=[{"name": "Ann", "email": "secret@"}]))

    assert "secret@" not in response.text


def test_duplicate_recipient_email_is_rejected(client: TestClient) -> None:
    recipients = [
        {"name": "Ann", "email": "same@example.com"},
        {"name": "Ann Again", "email": "SAME@example.com"},
    ]

    response = post_job(client, make_job_payload(recipients=recipients))

    assert response.status_code == 422
    assert "recipients[1] has the same email as recipients[0]" in response.text


def test_too_many_recipients_is_rejected(client: TestClient) -> None:
    recipients = make_recipients(*["Someone"] * (MAX_RECIPIENTS_PER_JOB + 1))

    response = post_job(client, make_job_payload(recipients=recipients))

    assert response.status_code == 422
    assert "body.recipients" in error_fields(response)


def test_malformed_json_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/jobs", content=b'{"recipients": [', headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- schema-level checks (no HTTP) ---------------------------------------------------------


def test_recipient_name_whitespace_is_collapsed() -> None:
    recipient = RecipientIn.model_validate({"name": "  Mary \n Ann  ", "email": "m@example.com"})

    assert recipient.name == "Mary Ann"


def test_recipient_email_is_lowercased() -> None:
    recipient = RecipientIn.model_validate({"name": "Mary", "email": "Mary@Example.com"})

    assert recipient.email == "mary@example.com"


def test_unicode_names_are_accepted_by_validation() -> None:
    recipient = RecipientIn.model_validate({"name": "José Álvarez", "email": "j@example.com"})

    assert recipient.name == "José Álvarez"


def test_decomposed_accents_are_normalized_to_composed_form() -> None:
    decomposed = "José"  # "e" followed by a combining acute accent

    recipient = RecipientIn.model_validate({"name": decomposed, "email": "j@example.com"})

    assert recipient.name == "José"


def test_recipient_names_the_font_cannot_draw_are_accepted() -> None:
    """They fail only their own certificate later (see test_failure_handling)."""
    job = JobCreate.model_validate(make_job_payload(recipients=make_recipients("李雷")))

    assert job.recipients[0].name == "李雷"


def test_valid_job_request_parses() -> None:
    job = JobCreate.model_validate(make_job_payload())

    assert job.event_date.isoformat() == "2026-10-07"
    assert len(job.recipients) == 2


def test_schema_rejects_empty_recipient_list() -> None:
    with pytest.raises(ValidationError):
        JobCreate.model_validate(make_job_payload(recipients=[]))
