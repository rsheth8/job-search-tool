# Job Search Intelligence — container image.
# Python 3.13 (NOT 3.14 — pydantic-core had no wheels at setup time).
FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install deps first so the layer caches across code-only changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# SQLite lives on a persistent volume in production (see fly.toml mounts), so it
# survives restarts/redeploys. init_db() runs at import and is idempotent.
ENV DATABASE_PATH=/data/job_search.db

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
