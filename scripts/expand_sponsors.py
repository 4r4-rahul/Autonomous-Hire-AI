#!/usr/bin/env python3
"""Grow the discovery universe: probe a candidate list of H1B-sponsoring companies against ALL
board APIs (Greenhouse/Lever/Ashby/Workable/SmartRecruiters via _fetch_company_postings) and add the
ones with a LIVE board carrying >=1 US role to data/board_sponsors.json (dedup, order-preserving).

Only companies with a reachable public board survive — so the list stays clean (no dead entries the
board scanner would waste a fetch on). Candidates are well-known H1B sponsors across tech/AI/data/
fintech/health/security/enterprise; the board check is what makes them count.

Usage: python scripts/expand_sponsors.py [--dry-run]
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))   # repo root, so `agents` imports when run directly

from agents.discovery.apply_url_resolver import _fetch_company_postings, loc_is_us_ok

_FILE = Path(__file__).parent.parent / "data" / "board_sponsors.json"

# Candidate H1B sponsors to test for a public board. Names already present are skipped. Kept broad —
# only those that actually resolve to a live US board get added.
_CANDIDATES = [
    # AI / ML
    "anthropic", "openai", "cohere", "mistral ai", "scale ai", "hugging face", "databricks",
    "weights & biases", "labelbox", "you.com", "glean", "sierra", "writer", "jasper", "tome",
    "harvey", "abridge", "cresta", "moveworks", "adept", "imbue", "contextual ai", "twelve labs",
    "elevenlabs", "synthesia", "runway", "pika", "luma ai", "suno", "midjourney", "ideogram",
    "deepgram", "assemblyai", "lightning ai", "octoml", "predibase", "baseten", "modal labs",
    "together ai", "fireworks ai", "anyscale", "lamini", "unstructured", "llamaindex", "langchain",
    "vespa", "qdrant", "zilliz", "chroma", "neon", "tigergraph",
    # Data infra / devtools
    "fivetran", "airbyte", "dbt labs", "starburst", "dremio", "sigma computing", "hex",
    "hightouch", "census", "monte carlo", "bigeye", "cube", "preset", "estuary", "rudderstack",
    "segment", "amplitude", "mixpanel", "heap", "posthog", "metabase", "mode", "thoughtspot",
    "temporal", "cockroach labs", "yugabyte", "planetscale", "singlestore", "materialize",
    "clickhouse", "timescale", "redpanda", "confluent", "streamsets", "prefect", "dagster",
    "astronomer", "mage", "windmill", "sourcegraph", "gitpod", "coder", "warp", "replit",
    "render", "railway", "fly.io", "vercel", "netlify", "supabase", "appwrite", "nhost",
    "pulumi", "spacelift", "env0", "harness", "circleci", "buildkite", "earthly",
    "postman", "hoppscotch", "kong", "tyk", "ngrok", "tailscale", "teleport",
    "grafana labs", "chronosphere", "honeycomb", "lightstep", "sentry", "rollbar", "bugsnag",
    # Fintech / payments
    "stripe", "plaid", "brex", "ramp", "mercury", "modern treasury", "unit", "column", "lithic",
    "increase", "highnote", "moov", "finix", "checkout.com", "rapyd", "marqeta", "galileo",
    "sofi", "chime", "dave", "varo", "current", "step", "greenlight", "betterment", "wealthfront",
    "public", "robinhood", "webull", "alpaca", "tradingview", "addepar", "carta", "angellist",
    "gusto", "rippling", "deel", "remote", "justworks", "trinet", "lattice", "deel",
    "affirm", "afterpay", "klarna", "sezzle", "bill", "melio", "tipalti", "ramp", "navan",
    # Crypto
    "coinbase", "kraken", "gemini", "circle", "chainalysis", "fireblocks", "anchorage digital",
    "paxos", "bitgo", "ripple", "uniswap labs", "alchemy", "dapper labs", "ledger",
    # Security
    "cloudflare", "okta", "auth0", "1password", "dashlane", "snyk", "wiz", "orca security",
    "lacework", "sysdig", "aqua security", "sentinelone", "crowdstrike", "abnormal security",
    "material security", "tessian", "vanta", "drata", "secureframe", "semgrep", "chainguard",
    "tailscale", "twingate", "cyberhaven", "huntress", "expel", "arctic wolf",
    # Health tech
    "benchling", "tempus", "flatiron health", "komodo health", "cedar", "ro", "hims",
    "devoted health", "cityblock health", "lyra health", "spring health", "headway", "alma",
    "garner health", "transcarent", "maven clinic", "carbon health", "color health", "verily",
    # Enterprise SaaS / infra
    "notion", "figma", "canva", "miro", "airtable", "linear", "asana", "monday.com", "clickup",
    "loom", "calendly", "front", "intercom", "drift", "gong", "chorus", "outreach", "salesloft",
    "apollo.io", "clay", "instabase", "glean", "dropbox", "box", "docusign", "pandadoc",
    "webflow", "framer", "contentful", "sanity", "storyblok", "builder.io", "vanta",
    "samsara", "verkada", "rivian", "lucid motors", "zoox", "nuro", "aurora", "applied intuition",
    "anduril", "shield ai", "saronic", "hadrian", "machina labs",  # may be ITAR — board check + US only still applies
    "ironclad", "everlaw", "relativity", "disco", "clio",
    "faire", "whatnot", "stockx", "goat", "thredup", "poshmark",
    "doordash", "instacart", "gopuff", "toast", "olo", "sweetgreen",
    "discord", "reddit", "pinterest", "nextdoor", "patreon", "substack", "medium", "ghost",
    "roblox", "unity", "epic games", "riot games", "scopely", "discord",
    "twilio", "sendgrid", "vonage", "bandwidth", "telnyx", "sinch",
    "datadog", "new relic", "dynatrace", "elastic", "grafana", "splunk",
    "hashicorp", "gitlab", "jfrog", "harness", "launchdarkly", "split", "statsig",
    "nvidia", "arm", "cerebras", "groq", "sambanova", "graphcore", "tenstorrent", "lightmatter",
    # Insurance / insurtech
    "lemonade", "root insurance", "hippo", "next insurance", "ethos", "newfront", "at-bay",
    "clearcover", "kin insurance", "pie insurance", "openly", "bestow", "ladder", "sure",
    "vouch", "cover genius", "trellis", "counterpart", "corvus insurance", "insurify",
    "policygenius", "everquote", "the zebra", "gabi", "marshmallow", "branch insurance",
    "loop insurance", "wefox", "getsafe", "l025", "boost insurance", "ledgebrook", "kettle",
    "shift technology", "tractable", "snapsheet", "cape analytics", "akur8", "planck",
    "assurance iq", "healthcare.com", "gravie", "sana benefits", "decent", "sidecar health",
    "clearwater analytics", "duck creek", "guidewire", "applied systems", "ethos life",
    # Healthcare / health tech / digital health
    "oscar health", "clover health", "included health", "hinge health", "sword health",
    "omada health", "virta health", "commure", "innovaccer", "cohere health", "notable health",
    "akasa", "truveta", "datavant", "particle health", "health gorilla", "zus health",
    "memora health", "bicycle health", "brightline", "brave health", "eleos health", "equip health",
    "big health", "modern health", "talkspace", "cerebral", "k health", "curai health",
    "buoy health", "aledade", "pearl health", "stellar health", "strive health", "monogram health",
    "somatus", "carrum health", "nuna", "clarify health", "abridge", "ambience healthcare",
    "suki", "regard", "navina", "fabric health", "qventus", "olive ai", "rivet health",
    "candid health", "tennr", "thoughtful ai", "machinify", "alaffia health", "anomaly",
    "recursion", "insitro", "tempus ai", "veeva", "veradigm", "doximity", "grow therapy",
    "headway health", "two chairs", "octave", "lyra health", "spring health", "rula",
    "garner health", "transcarent", "maven clinic", "carbon health", "color health", "ro health",
    "function health", "superpower", "parsley health", "tia", "midi health", "maximus health",
    "hims health", "openevidence", "abacus insights", "turquoise health", "collectivehealth",
    "wellsky", "particlehealth", "evolenthealth", "privia health", "tebra", "cedar health",
]


async def main() -> None:
    dry = "--dry-run" in sys.argv
    data = json.loads(_FILE.read_text())
    existing = {c.strip().lower() for c in data["companies"]}
    cand = [c for c in dict.fromkeys(_CANDIDATES) if c.strip().lower() not in existing]
    print(f"existing: {len(existing)} | new candidates to probe: {len(cand)}")

    sem = asyncio.Semaphore(10)

    async def _probe(name):
        async with sem:
            try:
                postings = await _fetch_company_postings(name)
            except Exception:
                return name, None
        if not postings:
            return name, None
        us = [p for p in postings if loc_is_us_ok(p.get("location", ""))]
        return name, {"total": len(postings), "us": len(us), "ats": postings[0].get("ats")}

    results = await asyncio.gather(*[_probe(c) for c in cand])
    live = [(n, info) for n, info in results if info and info["us"] > 0]
    live.sort(key=lambda x: -x[1]["us"])

    print(f"\nlive boards with >=1 US role: {len(live)}")
    for n, info in live:
        print(f"  + {n:28} {info['ats']:14} US={info['us']:>3}/{info['total']}")

    if dry:
        print("\n(dry-run — not written)")
        return
    if live:
        data["companies"].extend(n for n, _ in live)
        _FILE.write_text(json.dumps(data, indent=2))
        print(f"\nadded {len(live)} → board_sponsors.json now has {len(data['companies'])} companies")


if __name__ == "__main__":
    asyncio.run(main())
