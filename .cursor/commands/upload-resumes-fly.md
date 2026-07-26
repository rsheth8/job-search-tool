# Upload resumes to Fly

One-time backend step (needs `fly auth login`). The iOS **Download PDF** button and `GET /apply/resume` return 404 until base `.tex` files are on the Fly volume. They are gitignored personal files in this repo.

**Local files (confirmed paths):**
- `resumes/swe.tex` — software-engineering roles
- `resumes/aiml.tex` — ML / AI / data-science roles

**Remote:** app `job-search-tool`, volume dir `/data/resumes/` (`RESUME_TEX_DIR` in `fly.toml`).

Run from the project root (`/Users/rahilsheth/Documents/job-search-tool`):

```bash
cd /Users/rahilsheth/Documents/job-search-tool

fly ssh console -a job-search-tool -C "mkdir -p /data/resumes"

# fly ssh sftp put refuses to overwrite — rm first when updating
fly ssh console -a job-search-tool -C "rm -f /data/resumes/swe.tex /data/resumes/aiml.tex"

fly ssh sftp put -a job-search-tool \
  /Users/rahilsheth/Documents/job-search-tool/resumes/swe.tex \
  /data/resumes/swe.tex

fly ssh sftp put -a job-search-tool \
  /Users/rahilsheth/Documents/job-search-tool/resumes/aiml.tex \
  /data/resumes/aiml.tex

fly ssh console -a job-search-tool -C "ls -la /data/resumes/"
```

Expect `swe.tex` and `aiml.tex` in the listing (dates should match today). Do **not** use `scp root@*.fly.dev` — Fly closes that; use `fly ssh sftp put`.

After upload, the backend tailors `swe`/`aiml` per job (Claude edits → Tectonic compile → one page) and the resume button delivers the PDF.

If `fly` is not authenticated, run `fly auth login` first, then retry.
