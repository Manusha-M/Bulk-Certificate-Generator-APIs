import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models import CertificateStatus, JobStatus


class CertificateResult(BaseModel):
    id: uuid.UUID
    recipient_index: int = Field(description="Position of the recipient in the original request.")
    recipient_name: str
    recipient_email: str
    status: CertificateStatus
    download_url: str | None = Field(description="Only set when the certificate is COMPLETED.")
    error: str | None = Field(description="Only set when the certificate FAILED.")
    completed_at: datetime | None


class JobCertificatesResponse(BaseModel):
    job_id: uuid.UUID
    job_status: JobStatus
    count: int
    certificates: list[CertificateResult]
