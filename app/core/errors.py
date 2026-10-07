"""Application exceptions that map to HTTP error responses.

Services raise these; `app.api.error_handlers` turns them into the JSON error format.
Their messages are written for API clients, so they never contain paths or stack traces.
"""


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    """The resource exists but is not in a state that allows the request."""

    status_code = 409
    code = "conflict"


class CertificateFileUnavailableError(AppError):
    """The database says the certificate exists, but its file cannot be served."""

    status_code = 500
    code = "certificate_file_unavailable"
