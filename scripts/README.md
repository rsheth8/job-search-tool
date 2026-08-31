# Local helper scripts (not imported by the app)

Operational one-offs for migrating data and maintaining the ATS directory.
Run with the project venv:

```bash
.venv/bin/python -m scripts.<module> [args]
```

| Script | Purpose |
|---|---|
| `migrate_user.py` | Merge all rows from one user id into another (`local` → `usr_…`). `--dry-run` previews. |
| `users.py` | **Accounts: list, show, orphans, export, import, merge, delete.** Start here. |
| `export_user.py` | Export one user's "brain" (profile, identity, swipe labels, model) to a standalone SQLite file. |
| `import_user.py` | Import a brain file into the current DB under a chosen user id (safe on prod — no overwrites). |
| `build_ats_boards.py` | Probe candidate slugs and rebuild `data/ats_boards.json`. |
| `build_company_catalog.py` | Rebuild `data/company_catalog.json` (CMS hospitals, US universities, listed companies + known ATS tokens). |
| `validate_ats_boards.py` | Validate `data/ats_boards.json` or probe one slug on all ATS types. |
| `beta_preflight.py` | Check a live deployment against the invite-beta checklist. Exits 1 on a blocker. |
| `rescore.py` | Recompute `relevance_score` for postings already stored (free heuristic only). `--dry-run` previews. |

### Examples

```bash
# Who is in this database, and what do they own?
.venv/bin/python -m scripts.users list
.venv/bin/python -m scripts.users show usr_abc123
.venv/bin/python -m scripts.users orphans      # rows whose user id has no account

# Back one up, or remove one. `delete` backs up first and refuses without --yes.
.venv/bin/python -m scripts.users export usr_abc123 backup.db
.venv/bin/python -m scripts.users delete usr_abc123 --yes

# Export local training data, import on Fly under your Apple user id:
DATABASE_PATH=job_search.db .venv/bin/python -m scripts.export_user local brain.db
fly ssh sftp put -a job-search-tool brain.db /data/brain.db
fly ssh console -a job-search-tool -C "python -m scripts.import_user /data/brain.db usr_abc123"

# Merge split accounts:
.venv/bin/python -m scripts.migrate_user local usr_abc123 --dry-run

# A posting is scored once, at discovery. After a scorer change or a profile
# edit, existing rows keep their old numbers until this runs:
.venv/bin/python -m scripts.rescore --user usr_abc123 --dry-run
.venv/bin/python -m scripts.rescore --all

# Before inviting anyone — every paid path fails open, so a bad key or an
# empty /data/resumes degrades the product without turning /health red:
.venv/bin/python -m scripts.beta_preflight --url https://job-search-tool.fly.dev
# ...and to prove the Anthropic key is live (spends one tiny real call):
.venv/bin/python -m scripts.beta_preflight --url https://job-search-tool.fly.dev \
    --token "$APPLY_API_TOKEN" --spend
```

On Fly, `fly ssh console -C` execs the binary directly: there is no shell, so
`cd /app && …` fails with `exec: "cd": executable file not found in $PATH`. It
already starts in the image's WORKDIR (`/app`), so just run the module:
`fly ssh console -a job-search-tool -C "python -m scripts.X …"`. Need shell
syntax? Ask for a shell: `-C "/bin/sh -c 'a && b'"`.
