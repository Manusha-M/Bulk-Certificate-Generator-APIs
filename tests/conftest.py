"""Shared fixtures.

Tests run against a real PostgreSQL database given by TEST_DATABASE_URL (environment or
`.env`). As a safety net the database name must end in "_test", so the suite can never
wipe a development database. The schema is built with the real Alembic migration.
"""

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_certificate_generator
from app.core.config import PROJECT_ROOT, Settings
from app.db.database import create_db_engine, create_session_factory
from app.main import create_app
from app.services.certificate_generator import (
    CertificateData,
    CertificateGenerationError,
    CertificateGenerator,
)

TEMPLATE_PATH = PROJECT_ROOT / "templates" / "certificate_template.png"


def make_job_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "certificate_title": "Certificate of Completion",
        "course_name": "Advanced Python Backend Development",
        "organization_name": "Example Academy",
        "event_date": "2026-10-07",
        "recipients": [
            {"name": "John Doe", "email": "john@example.com"},
            {"name": "Jane Smith", "email": "jane@example.com"},
        ],
    }
    payload.update(overrides)
    return payload


def make_recipients(*names: str) -> list[dict[str, str]]:
    return [
        {"name": name, "email": f"recipient{index}@example.com"} for index, name in enumerate(names)
    ]


class FailingGenerator(CertificateGenerator):
    """Real generator that raises `error` for the recipients named in `fail_for`."""

    def __init__(
        self, output_dir: Path, fail_for: set[str], error: Exception | None = None
    ) -> None:
        super().__init__(TEMPLATE_PATH, output_dir)
        self.fail_for = fail_for
        self.error = error or CertificateGenerationError("Simulated generation failure")

    def generate_certificate(self, data: CertificateData) -> str:
        if data.recipient_name in self.fail_for:
            raise self.error
        return super().generate_certificate(data)


# --- database -----------------------------------------------------------------------------


def _test_database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or dotenv_values(PROJECT_ROOT / ".env").get(
        "TEST_DATABASE_URL"
    )


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()
    if not url:
        pytest.fail(
            "TEST_DATABASE_URL is not set. Start PostgreSQL (docker compose up -d db) and set "
            "TEST_DATABASE_URL in the environment or .env (see .env.example).",
            pytrace=False,
        )
    database_name = make_url(url).database or ""
    if not database_name.endswith("_test"):
        pytest.fail(
            f"Refusing to run tests against database {database_name!r}: "
            "the test database name must end with '_test'.",
            pytrace=False,
        )
    return url


def _alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    """Rebuild the test schema from scratch with the Alembic migrations (down, then up)."""
    config = _alembic_config(database_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_db_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Empty tables before each test. Truncation (not a rolled-back transaction) is needed
    because background processing commits through its own sessions."""
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE certificates, generation_jobs CASCADE"))
    return create_session_factory(engine)


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


# --- certificate generation ---------------------------------------------------------------


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    return tmp_path / "generated"


@pytest.fixture
def generator(output_dir: Path) -> CertificateGenerator:
    return CertificateGenerator(TEMPLATE_PATH, output_dir)


# --- application --------------------------------------------------------------------------


@pytest.fixture
def settings(database_url: str, output_dir: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url=database_url,
        output_dir=output_dir,
        template_path=TEMPLATE_PATH,
    )


@pytest.fixture
def app(settings: Settings, session_factory: sessionmaker[Session]) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # TestClient runs background tasks before `client.post(...)` returns, so a created
    # job has already been processed when the test continues.
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def use_generator(app: FastAPI) -> Callable[[CertificateGenerator], None]:
    """Swap the generator used by the API (dependency override)."""

    def override(generator: CertificateGenerator) -> None:
        app.dependency_overrides[get_certificate_generator] = lambda: generator

    return override


@pytest.fixture
def create_job_via_api(client: TestClient) -> Callable[..., dict[str, Any]]:
    def create(**overrides: Any) -> dict[str, Any]:
        response = client.post("/api/v1/jobs", json=make_job_payload(**overrides))
        assert response.status_code == 202, response.text
        return response.json()

    return create


def random_uuid() -> str:
    return str(uuid.uuid4())
