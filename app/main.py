"""Application factory.

Run with:  uvicorn app.main:create_app --factory --reload
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI

from app.api.error_handlers import register_error_handlers
from app.api.routes import api_router
from app.api.routes.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.database import create_db_engine, create_session_factory
from app.services.certificate_generator import CertificateGenerator

API_PREFIX = "/api/v1"

DESCRIPTION = """
Submit one request with many recipients; the API generates one PDF certificate per
recipient in the background, tracks progress, and serves the generated files.

1. `POST /api/v1/jobs` — create a job (returns immediately with a `job_id`)
2. `GET /api/v1/jobs/{job_id}` — poll status and progress
3. `GET /api/v1/jobs/{job_id}/certificates` — per-recipient results
4. `GET /api/v1/certificates/{certificate_id}` — download a PDF
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    output_dir = settings.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = settings.model_copy(update={"output_dir": output_dir})

    engine = create_db_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Created here because it must belong to the running event loop.
        app.state.job_limiter = anyio.CapacityLimiter(settings.max_concurrent_jobs)
        yield
        engine.dispose()

    app = FastAPI(
        title="Bulk Certificate Generator API",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = create_session_factory(engine)
    app.state.certificate_generator = CertificateGenerator(settings.template_path, output_dir)

    register_error_handlers(app)
    app.include_router(api_router, prefix=API_PREFIX)
    app.include_router(health_router)
    return app
