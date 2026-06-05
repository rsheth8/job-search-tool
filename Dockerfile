# Job Search Intelligence — container image.
# Python 3.13 (NOT 3.14 — pydantic-core had no wheels at setup time).
FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install deps first so the layer caches across code-only changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Tectonic — single-binary LaTeX engine for resume compile + page check.
ARG TECTONIC_VERSION=0.15.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL \
       "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-x86_64-unknown-linux-gnu.tar.gz" \
       -o /tmp/tectonic.tgz \
    && tar -xzf /tmp/tectonic.tgz -C /usr/local/bin \
    && rm /tmp/tectonic.tgz \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# SQLite lives on a persistent volume in production (see fly.toml mounts), so it
# survives restarts/redeploys. init_db() runs at import and is idempotent.
ENV DATABASE_PATH=/data/job_search.db

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
