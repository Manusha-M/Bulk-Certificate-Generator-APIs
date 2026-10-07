from fastapi import APIRouter

from app.api.routes import certificates, jobs

api_router = APIRouter()
api_router.include_router(jobs.router)
api_router.include_router(certificates.router)
