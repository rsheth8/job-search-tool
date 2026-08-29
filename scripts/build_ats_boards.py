#!/usr/bin/env python3
"""Build data/ats_boards.json by probing public ATS boards.

Merges new hits into the existing JSON (does not drop boards that already
passed). Greenhouse / Lever / Ashby slugs are assigned to one source;
SmartRecruiters / Workable identifiers are probed separately.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_ats_boards import probe_slug  # noqa: E402

# Curated slugs: big tech + mid-size / SMB growth companies (AI, devtools, fintech).
# Each slug is probed on all three ATS types; valid hits are kept (deduped per source).
CANDIDATE_SLUGS = [
    # --- already in rotation (big) ---
    "stripe", "airbnb", "discord", "figma", "notion", "cloudflare", "datadog",
    "hashicorp", "gitlab", "doordash", "instacart", "coinbase", "plaid", "gusto",
    "brex", "scale", "anduril", "databricks", "snowflake", "mongodb", "elastic",
    "asana", "dropbox", "lyft", "pinterest", "reddit", "robinhood", "sofi",
    "palantir", "netflix", "spotify", "atlassian", "canva", "rippling",
    "vercel", "loom", "flexport", "affirm", "chime", "glossier", "faire",
    "ramp", "linear", "anthropic", "openai", "retool", "deel", "mercury",
    "vanta", "cognition", "harvey", "sierra",
    # --- devtools / infra (mid-size) ---
    "posthog", "supabase", "temporal", "dagster", "prefect", "netlify",
    "tailscale", "1password", "sentry", "circleci", "buildkite", "honeycomb",
    "grafana", "cockroachlabs", "timescale", "materialize", "dbt-labs",
    "fivetran", "airtable", "clickup", "pulumi", "spacelift", "verkada",
    "planetscale", "neon", "fly", "render", "resend", "clerk", "cal",
    "statsig", "launchdarkly", "amplitude", "mixpanel", "codecov",
    # --- AI / ML ---
    "huggingface", "wandb", "cerebras", "together", "pinecone", "weaviate",
    "langchain", "anyscale", "modal", "replicate", "assemblyai", "deepgram",
    "labelbox", "runpod", "elevenlabs", "perplexity", "cursor", "poolside",
    "codeium", "sourcegraph", "suno", "fal", "greptile", "decagon",
    # --- fintech / B2B ---
    "modern-treasury", "column", "lithic", "snyk", "wiz", "commonroom",
    "incident", "firehydrant", "dub",
    # --- lever-leaning / other ---
    "duckduckgo", "zapier", "miro", "segment", "twilio", "yelp", "box",
    "zendesk", "unity", "roblox", "gopuff", "convoy", "navan",
    # --- autonomy / hard tech ---
    "nuro", "aurora", "appliedintuition", "waymo", "zoox", "cruise",
    # --- extra SMB / growth ---
    "hex", "pylon", "plain", "incident-io", "rootly",
    "mintlify", "inkeep", "braintrust", "langfuse", "literalai",
    "tigris", "upstash", "turso", "deno", "bun", "astro", "sanity",
    "prisma", "hasura", "nhost", "appwrite",
    "arc", "unit", "moov",
    # --- extra coverage (re-probe if missing from the JSON) ---
    "anduril", "appliedintuition", "baseten", "block", "canva",
    "clerk", "confluent", "cribl", "duolingo", "grammarly",
    "hubspot", "huggingface", "intercom", "klaviyo", "mechanize",
    "pagerduty", "retool", "rippling", "shopify", "together",
    "uipath", "veeva", "wiz", "workos", "anaplan", "matchgroup",
    "rocketlab", "sourcegraph", "okta", "duolingo",
    "andurilindustries", "scaleai",
]

# Identifiers that are not interchangeable with GH/Lever/Ashby slugs.
SMARTRECRUITERS_CANDIDATES = [
    "ServiceNow", "WesternDigital", "Thales", "BoschGroup", "Experian",
    "Intuitive", "Sandisk", "AbbVie", "PaloAltoNetworks2", "NBCUniversal3",
]
WORKABLE_CANDIDATES = [
    "grayce", "thorlabs", "qodeworld",
]

OUTPUT = ROOT / "data" / "ats_boards.json"


def _merge(out: dict[str, list[str]], source: str, token: str) -> bool:
    lst = out.setdefault(source, [])
    if token.lower() in {t.lower() for t in lst}:
        return False
    lst.append(token)
    return True


def main() -> int:
    existing: dict[str, list[str]] = {
        "greenhouse": [], "lever": [], "ashby": [],
        "workable": [], "smartrecruiters": [],
    }
    if OUTPUT.exists():
        try:
            data = json.loads(OUTPUT.read_text(encoding="utf-8"))
            for src in existing:
                existing[src] = [str(t).strip() for t in (data.get(src) or []) if str(t).strip()]
        except (OSError, json.JSONDecodeError):
            pass

    assigned = {t.lower() for lst in existing.values() for t in lst}

    seen_slugs: set[str] = set()
    slugs: list[str] = []
    for raw in CANDIDATE_SLUGS:
        s = raw.strip().lower()
        if s and s not in seen_slugs:
            seen_slugs.add(s)
            slugs.append(s)

    to_probe = [s for s in slugs if s not in assigned]
    source_order = ("ashby", "greenhouse", "lever")

    for i, slug in enumerate(to_probe, 1):
        print(f"[{i}/{len(to_probe)}] {slug} ...", flush=True)
        hits = probe_slug(slug, sources=("greenhouse", "lever", "ashby"))
        if not hits:
            print("  — no board", flush=True)
            continue
        hits_by_source = {s: n for s, n in hits}
        chosen = next((s for s in source_order if s in hits_by_source), hits[0][0])
        n = hits_by_source[chosen]
        if _merge(existing, chosen, slug):
            assigned.add(slug)
            print(f"  + {chosen} ({n} jobs)", flush=True)
        else:
            print(f"  = already in {chosen}", flush=True)

    print("\nSmartRecruiters identifiers ...", flush=True)
    for token in SMARTRECRUITERS_CANDIDATES:
        hits = probe_slug(token, sources=("smartrecruiters",))
        if hits:
            jobs = hits[0][1]
            if _merge(existing, "smartrecruiters", token):
                print(f"  + smartrecruiters {token} ({jobs} jobs)", flush=True)
            else:
                print(f"  = {token}", flush=True)
        else:
            print(f"  — {token}", flush=True)

    print("\nWorkable accounts ...", flush=True)
    for token in WORKABLE_CANDIDATES:
        hits = probe_slug(token, sources=("workable",))
        if hits:
            jobs = hits[0][1]
            if _merge(existing, "workable", token.lower()):
                print(f"  + workable {token} ({jobs} jobs)", flush=True)
            else:
                print(f"  = {token}", flush=True)
        else:
            print(f"  — {token}", flush=True)

    for source in existing:
        existing[source] = sorted(existing[source], key=lambda t: t.lower())

    OUTPUT.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    total = sum(len(v) for v in existing.values())
    print(f"\nWrote {total} boards to {OUTPUT}")
    for source, boards in existing.items():
        print(f"  {source}: {len(boards)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
