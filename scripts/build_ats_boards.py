#!/usr/bin/env python3
"""Build data/ats_boards.json by probing slugs on Greenhouse, Lever, and Ashby."""
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
]

OUTPUT = ROOT / "data" / "ats_boards.json"


def main() -> int:
    seen_slugs: set[str] = set()
    slugs: list[str] = []
    for raw in CANDIDATE_SLUGS:
        s = raw.strip().lower()
        if s and s not in seen_slugs:
            seen_slugs.add(s)
            slugs.append(s)

    out: dict[str, list[str]] = {"greenhouse": [], "lever": [], "ashby": []}
    seen_board: set[tuple[str, str]] = set()
    assigned_slug: set[str] = set()
    # Prefer Ashby (common for SMBs), then Greenhouse, then Lever.
    source_order = ("ashby", "greenhouse", "lever")

    for i, slug in enumerate(slugs, 1):
        print(f"[{i}/{len(slugs)}] {slug} ...", flush=True)
        hits = probe_slug(slug)
        if not hits:
            print(f"  — no board", flush=True)
            continue
        if slug in assigned_slug:
            print(f"  — already assigned", flush=True)
            continue
        hits_by_source = {s: n for s, n in hits}
        chosen = next((s for s in source_order if s in hits_by_source), hits[0][0])
        n = hits_by_source[chosen]
        key = (chosen, slug)
        seen_board.add(key)
        assigned_slug.add(slug)
        out[chosen].append(slug)
        print(f"  + {chosen} ({n} jobs)", flush=True)

    for source in out:
        out[source].sort()

    OUTPUT.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    total = sum(len(v) for v in out.values())
    print(f"\nWrote {total} boards to {OUTPUT}")
    for source, boards in out.items():
        print(f"  {source}: {len(boards)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
