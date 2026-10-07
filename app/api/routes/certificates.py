import uuid

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.api.dependencies import DbSession, SettingsDep
from app.schemas.error import ErrorResponse
from app.services import certificate_service

router = APIRouter(prefix="/certificates", tags=["Certificates"])


@router.get(
    "/{certificate_id}",
    name="download_certificate",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "The certificate PDF"},
        404: {"model": ErrorResponse, "description": "Certificate not found"},
        409: {"model": ErrorResponse, "description": "Certificate failed or is not ready yet"},
        422: {"model": ErrorResponse, "description": "Invalid certificate ID"},
    },
    summary="Download a generated certificate (PDF)",
)
def download_certificate(
    certificate_id: uuid.UUID, session: DbSession, settings: SettingsDep
) -> FileResponse:
    certificate = certificate_service.get_certificate(session, certificate_id)
    path = certificate_service.get_certificate_file(certificate, settings.output_dir)
    return FileResponse(
        path, media_type="application/pdf", filename=f"certificate-{certificate.id}.pdf"
    )
