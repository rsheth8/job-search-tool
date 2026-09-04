# Contributing to JobPilot

## Prerequisites
- Python **3.12 or 3.13** (not 3.14 — pydantic-core wheels)
- Optional: Xcode for the iOS app

## Run (CLI, no keys)
```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python cli.py
```

## Run (API)
```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Without `ANTHROPIC_API_KEY` the heuristic router stays offline.

## Tests
```bash
.venv/bin/python -m pytest -q
```

The suite is fully offline (~1,050 tests). Do not point tests at live ATS sites.

## Secrets
Never commit `resumes/*.tex` with personal info, `.env`, or Apple keys.
See `docs/DEPLOYMENT.md` and `deploy/BETA.md` for TestFlight.
