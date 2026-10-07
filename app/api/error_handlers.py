"""Turn every error into the same JSON shape: {"error": {"code", "message", "details"}}.

Internal details (stack traces, SQL, file paths) are logged, never returned.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError
from app.schemas.error import ErrorBody, ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)

HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(
        status_code=status_code, content=body.model_dump(exclude_none=True), headers=headers
    )


async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    if exc.status_code >= 500:
        logger.error("%s %s failed: %s", request.method, request.url.path, exc.message)
    return error_response(exc.status_code, exc.code, exc.message)


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # The rejected input values are deliberately not echoed back.
    details = [
        ErrorDetail(field=".".join(str(part) for part in error["loc"]), message=error["msg"])
        for error in exc.errors()
    ]
    return error_response(422, "validation_error", "Request validation failed", details)


async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    # Keep headers such as "Allow" on a 405.
    return error_response(exc.status_code, code, str(exc.detail), headers=exc.headers)


async def handle_database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.exception("Database error during %s %s", request.method, request.url.path)
    return error_response(500, "database_error", "A database error occurred")


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error during %s %s", request.method, request.url.path)
    return error_response(500, "internal_error", "An unexpected error occurred")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, handle_http_error)  # type: ignore[arg-type]
    app.add_exception_handler(SQLAlchemyError, handle_database_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, handle_unexpected_error)
