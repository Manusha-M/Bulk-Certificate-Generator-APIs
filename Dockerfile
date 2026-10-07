FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY app ./app
COPY templates ./templates

# Run as an unprivileged user; it only needs to write to the output directory.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/generated \
    && chown appuser:appuser /app/generated
USER appuser

ENV OUTPUT_DIR=/app/generated
EXPOSE 8000

# Bring the schema up to date, then start the API.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000"]
