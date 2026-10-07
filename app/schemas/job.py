import uuid
from datetime import date, datetime
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.db.models import (
    CERTIFICATE_TITLE_MAX_LENGTH,
    COURSE_NAME_MAX_LENGTH,
    ORGANIZATION_NAME_MAX_LENGTH,
    JobStatus,
)
from app.schemas.recipient import RecipientIn
from app.services.certificate_generator import font_can_display
from app.utils.text import normalize_text

MAX_RECIPIENTS_PER_JOB = 1000

CertificateTitle = Annotated[
    str,
    BeforeValidator(normalize_text),
    Field(min_length=3, max_length=CERTIFICATE_TITLE_MAX_LENGTH),
]
CourseName = Annotated[
    str, BeforeValidator(normalize_text), Field(min_length=2, max_length=COURSE_NAME_MAX_LENGTH)
]
OrganizationName = Annotated[
    str,
    BeforeValidator(normalize_text),
    Field(min_length=2, max_length=ORGANIZATION_NAME_MAX_LENGTH),
]

EXAMPLE_JOB_REQUEST = {
    "certificate_title": "Certificate of Completion",
    "course_name": "Advanced Python Backend Development",
    "organization_name": "Example Academy",
    "event_date": "2026-10-07",
    "recipients": [
        {"name": "John Doe", "email": "john@example.com"},
        {"name": "Jane Smith", "email": "jane@example.com"},
    ],
}


class JobCreate(BaseModel):
    """A bulk request: shared certificate details plus the list of recipients."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [EXAMPLE_JOB_REQUEST]})

    certificate_title: CertificateTitle
    course_name: CourseName
    organization_name: OrganizationName
    event_date: date
    recipients: list[RecipientIn] = Field(min_length=1, max_length=MAX_RECIPIENTS_PER_JOB)

    @field_validator("certificate_title", "course_name", "organization_name")
    @classmethod
    def check_font_support(cls, value: str) -> str:
        """These texts appear on every certificate (title and organisation in upper case).

        If the font cannot draw them, every certificate would fail, so reject the request
        now. A recipient name the font cannot draw only fails that one certificate.
        """
        if not (font_can_display(value) and font_can_display(value.upper())):
            raise ValueError("contains characters that the certificate font cannot display")
        return value

    @model_validator(mode="after")
    def reject_duplicate_recipients(self) -> Self:
        """The same email twice in one job would produce a duplicate certificate."""
        first_index_by_email: dict[str, int] = {}
        for index, recipient in enumerate(self.recipients):
            if recipient.email in first_index_by_email:
                raise ValueError(
                    f"recipients[{index}] has the same email as "
                    f"recipients[{first_index_by_email[recipient.email]}]"
                )
            first_index_by_email[recipient.email] = index
        return self


class JobCreatedResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    total_recipients: int
    status_url: str = Field(description="Poll this URL to follow the job's progress.")
    certificates_url: str = Field(description="Per-recipient results and download links.")


class JobStatusResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    is_finished: bool = Field(description="True once the job reached a final status.")
    total: int
    completed: int = Field(description="Certificates generated successfully.")
    failed: int = Field(description="Certificates that could not be generated.")
    pending: int = Field(description="Certificates not processed yet.")
    progress_percentage: float = Field(description="(completed + failed) / total * 100.")
    certificate_title: str
    course_name: str
    organization_name: str
    event_date: date
    error: str | None = Field(description="Job-level failure reason, if the job failed.")
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    certificates_url: str
