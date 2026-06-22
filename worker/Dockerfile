# Phase 2 submit worker — headless Chromium via Playwright.
# Deployed as a SEPARATE Fly app from the main 512MB box (this needs ~1-2GB and
# scales to zero when idle). Reuses app/fieldmatch.py from the repo.
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY worker/requirements.txt worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt

# Bring in the app package (for app.fieldmatch) + the worker.
COPY app app
COPY worker worker

# Long-running poller; processes one approval at a time.
CMD ["python", "-m", "worker.run"]
