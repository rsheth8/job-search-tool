# Resume corpus

Real PDFs with hand-checked expected output, run by `tests/test_resume_corpus.py`.

## Why PDFs and not strings

Every resume bug found so far happened *before* any regex ran:

- pypdf's default extraction welded a right-aligned date onto the line's last
  word — `Minneapolis, MNAug 2023` — so `Minneapolis, MN` was unmatchable;
- the same extraction split a letter-spaced heading into `EDUCA TION`, so the
  education section was invisible and every field under it was looked up
  against the whole document instead;
- a sidebar layout put both columns on each physical line, so no heading was
  ever at the start of one.

A fixture written as a Python string cannot reproduce any of that. It starts
downstream of the damage. So these are PDFs.

## Layout

```
hfill_dates.pdf      the resume
hfill_dates.json     gold: the fields a person reading it would write down
src/hfill_dates.tex  the LaTeX it was built from
```

The `.tex` sources are here so a fixture is reviewable in a diff and can be
rebuilt, rather than being an opaque binary nobody dares touch. CI has no LaTeX,
which is why the PDFs are committed rather than generated at test time.

## Adding one

1. Drop `<name>.pdf` in this directory. It is picked up automatically.
2. Write `<name>.json` beside it.
3. Run `python -m pytest tests/test_resume_corpus.py`.

**Write the gold from the resume, not from the parser.** Read the PDF, write
down what is true, then run the test. If it fails, you have found a bug — that
is the corpus working. Pasting in current output produces a test that can only
ever confirm today's behaviour, including today's bugs.

Use fictional people. These files are committed.

## Gold format

```json
{
  "note":     "what this fixture guards, and what it used to get wrong",
  "identity": { "city": "Austin", "school": "University of Texas at Austin" },
  "locations": "Austin, TX",
  "absent":   ["gpa"],
  "xfail":    { "discipline": "why this one cannot pass yet" }
}
```

- **`note`** is required. A fixture nobody can explain is a fixture nobody can
  safely change.
- **`identity`** fields must match exactly. Listing only some is fine.
- **`absent`** fields must come back empty. This is the important half: these
  values get autofilled onto real job applications, so a parser that invents a
  GPA is worse than one that leaves the box alone.
- **`xfail`** records a known gap with its reason. It is checked in the other
  direction too — if an xfail field starts matching, the suite fails and tells
  you to delete the note, so a fixed gap cannot sit here misdescribing the code.

## What each fixture covers

| fixture | layout | guards |
|---|---|---|
| `hfill_dates` | right-aligned dates, compound heading | the original bug: `React`/`AI` as a location, invisible education section, enrolment year read as graduation year |
| `two_column` | sidebar | column splitting; without it, a truncated school, an invented `B.A.`, and an employment year as the graduation year |
| `plain_single_column` | plain, one column | regression guard — the layout that always worked must keep working |
| `colon_headings` | `Education:`, `Class of 2025` | trailing-colon headings, and no GPA to invent |
| `ongoing_phd` | unfinished degree, no end date | that `grad_year` stays **empty** rather than borrowing a year from the experience section |

## Rebuilding a PDF

```bash
cd tests/fixtures/resumes/src && pdflatex -interaction=nonstopmode <name>.tex && mv <name>.pdf ..
```
