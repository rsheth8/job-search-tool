# Personal resume files — not committed (see .gitignore)

Place your base LaTeX resumes here for local dev:

| File | Use |
|---|---|
| `swe.tex` | Software-engineering roles |
| `aiml.tex` | ML / AI / data-science roles |

Reference PDFs (`*.pdf`) are fine to keep locally for comparison; they are not committed.

## Production (Fly.io)

Base `.tex` files live on the **persistent volume**, not in the Docker image:

```bash
fly ssh console -a job-search-tool -C "mkdir -p /data/resumes"
fly ssh sftp put -a job-search-tool resumes/swe.tex /data/resumes/swe.tex
fly ssh sftp put -a job-search-tool resumes/aiml.tex /data/resumes/aiml.tex
```

Set `RESUME_TEX_DIR=/data/resumes` on Fly (already in `fly.toml`).

## Tailored cache

When you `apply <#>`, the bot saves one-page tailored PDFs + `.tex` under:

```
$RESUME_TEX_DIR/tailored/<your_slack_user_id>/
```

Reuses cached resumes for the same company/role — no re-generation unless needed.

## Smoke tests

```bash
.venv/bin/python3 scripts/test_slack_upload.py --scopes-only
SLACK_TEST_USER=U0... .venv/bin/python3 scripts/test_slack_upload.py
```

Requires Slack bot scopes: `chat:write`, `files:write`.
