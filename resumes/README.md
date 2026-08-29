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

When you stage/apply to a posting, the engine saves one-page tailored PDFs + `.tex` under:

```
$RESUME_TEX_DIR/tailored/<user_id>/
```

Reuses a cached résumé only for the same posting, or the same company + title
+ job-description hash. A nearby title at the same company is tailored again.

## How testers get the PDF

- **iOS:** the apply browser pre-downloads the tailored PDF (`GET /apply/resume?posting_id=…`)
  and opens the share sheet — you still attach it manually in the WebView (iOS cannot
  set `<input type=file>`). Cover letters are the same documents menu, built on tap
  (`GET /apply/cover`), not on every Preflight.
- **Chat / owner tools:** same endpoint, or download from the `/apply` review page.

There is no Slack attachment path — delivery is in-app only.
