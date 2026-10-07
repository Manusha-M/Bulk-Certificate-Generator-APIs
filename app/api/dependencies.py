"""FastAPI dependencies. Everything comes from `app.state`, set once in `create_app`.

Tests swap these out with `app.dependency_overrides` (for example, a failing generator).
"""

from collections.abc import Iterator
from typing import Annotated

from anyio import CapacityLimiter
from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.services.certificate_generator import CertificateGenerator


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session_factory(request: Request) -> sessionmaker[Session]:
    return request.app.state.session_factory


def get_db(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> Iterator[Session]:
    """One session per request, always closed (and rolled back if uncommitted) afterwards."""
    with session_factory() as session:
        yield session


def get_certificate_generator(request: Request) -> CertificateGenerator:
    return request.app.state.certificate_generator


def get_job_limiter(request: Request) -> CapacityLimiter:
    return request.app.state.job_limiter


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DbSession = Annotated[Session, Depends(get_db)]
SessionFactoryDep = Annotated[sessionmaker[Session], Depends(get_session_factory)]
GeneratorDep = Annotated[CertificateGenerator, Depends(get_certificate_generator)]
JobLimiterDep = Annotated[CapacityLimiter, Depends(get_job_limiter)]
