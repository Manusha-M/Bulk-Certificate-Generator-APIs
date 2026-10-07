"""Cross-cutting API behaviour: documentation, health check, consistent error responses."""

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db


def test_openapi_documentation_is_available(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()

    assert schema["info"]["title"] == "Bulk Certificate Generator API"
    assert set(schema["paths"]) >= {
        "/api/v1/jobs",
        "/api/v1/jobs/{job_id}",
        "/api/v1/jobs/{job_id}/certificates",
        "/api/v1/certificates/{certificate_id}",
    }
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_health_check(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "database": "ok"}


def test_unknown_route_uses_error_format(client: TestClient) -> None:
    response = client.get("/api/v1/nothing-here")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_wrong_method_uses_error_format(client: TestClient) -> None:
    response = client.delete("/api/v1/jobs")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert response.headers["Allow"] == "POST"


class UnreachableDatabaseSession(Session):
    """Behaves like a session whose database connection is down."""

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise OperationalError("SELECT ...", {}, Exception("connection refused for nobody:secret"))


def unreachable_database_session() -> Iterator[Session]:
    with UnreachableDatabaseSession() as session:
        yield session


def test_database_errors_return_generic_500(app: FastAPI) -> None:
    app.dependency_overrides[get_db] = unreachable_database_session

    with TestClient(app) as client:
        response = client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "database_error", "message": "A database error occurred"}
    }
    assert "secret" not in response.text


def test_health_check_reports_database_down(app: FastAPI) -> None:
    app.dependency_overrides[get_db] = unreachable_database_session

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503


def test_unexpected_errors_return_generic_500(app: FastAPI) -> None:
    def broken_session() -> Iterator[Session]:
        raise RuntimeError("something internal at C:\\secret\\path")
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = broken_session

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "An unexpected error occurred"}
    }
