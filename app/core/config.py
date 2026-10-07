"""Application settings, read from environment variables (and an optional `.env` file)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = Field(
        description="SQLAlchemy database URL, e.g. postgresql+psycopg://user:password@host:5432/db",
    )
    output_dir: Path = Field(
        default=PROJECT_ROOT / "generated",
        description="Directory where generated PDFs are stored. Relative paths are resolved "
        "against the current working directory.",
    )
    template_path: Path = PROJECT_ROOT / "templates" / "certificate_template.png"
    max_concurrent_jobs: int = Field(
        default=2, ge=1, description="How many jobs may generate certificates at the same time."
    )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings (read once, then cached)."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
