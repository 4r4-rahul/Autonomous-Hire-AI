#!/usr/bin/env python3
"""Probe a banking/finance/fintech H1B-sponsor cohort against the board APIs and report which have a
LIVE US board (so they're worth adding to data/board_sponsors.json for the board scanner).

Reuses the same probe as scripts/expand_sponsors.py. Companies already in board_sponsors.json are
skipped. Pass --apply to append the live ones (order-preserving dedup).

Run: PYTHONPATH=. .venv/bin/python scripts/probe_finance_sponsors.py [--apply]
"""
from __future__ import annotations
import asyncio, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.discovery.apply_url_resolver import _fetch_company_postings, loc_is_us_ok

_FILE = Path(__file__).parent.parent / "data" / "board_sponsors.json"

# Banking / finance / fintech firms with a track record of H1B sponsorship. Broad on purpose —
# only those resolving to a LIVE US board survive the probe.
_CANDIDATES = [
    # Bulge-bracket / global banks (mostly Workday/custom — probe anyway, cheap)
    "goldman sachs", "morgan stanley", "jpmorgan chase", "jpmorgan", "bank of america",
    "citi", "citigroup", "wells fargo", "barclays", "deutsche bank", "ubs", "hsbc",
    "nomura", "rbc capital markets", "bnp paribas", "societe generale", "macquarie",
    "td securities", "bmo", "mizuho", "santander", "jefferies", "evercore", "lazard",
    "houlihan lokey", "moelis", "guggenheim", "raymond james", "stifel", "william blair",
    # US consumer / regional banks & lenders
    "american express", "us bank", "pnc", "truist", "fifth third bank", "citizens bank",
    "ally financial", "discover financial", "synchrony", "keybank", "regions bank",
    "m&t bank", "huntington bank", "comerica", "first citizens bank", "navy federal",
    # Asset managers
    "blackrock", "vanguard", "fidelity investments", "state street", "pimco", "invesco",
    "t rowe price", "franklin templeton", "wellington management", "nuveen", "capital group",
    "allianceberstein", "alliancebernstein", "dodge & cox", "dimensional fund advisors",
    "northern trust", "bny mellon", "principal financial", "voya financial", "janus henderson",
    # Private equity / alternatives / credit
    "blackstone", "kkr", "apollo global", "carlyle group", "ares management", "brookfield",
    "tpg", "blue owl capital", "general atlantic", "vista equity partners", "thoma bravo",
    "silver lake", "sixth street", "hps investment partners", "golub capital", "oaktree",
    "warburg pincus", "advent international", "bain capital", "insight partners", "coatue",
    # Exchanges / market infra / financial data & analytics
    "nasdaq", "cme group", "intercontinental exchange", "cboe global markets", "s&p global",
    "moody's", "moodys", "msci", "factset", "morningstar", "fitch ratings", "broadridge",
    "dtcc", "ss&c technologies", "clearwater analytics", "tradeweb", "marketaxess",
    "bloomberg", "refinitiv", "ice mortgage technology", "enfusion", "iex",
    # Payments / card networks / financial services tech
    "visa", "mastercard", "paypal", "fiserv", "fis global", "global payments", "jack henry",
    "adyen", "checkout.com", "wise", "remitly", "payoneer", "nuvei", "shift4", "worldpay",
    "toast", "square", "airwallex", "nium", "rapyd", "corpay", "flywire", "priority technology",
    # Fintech / neobank / lending / wealth / infra (Greenhouse/Lever/Ashby likely)
    "stash", "acorns", "m1 finance", "wealthsimple", "monzo", "revolut", "n26", "nubank",
    "bluevine", "fundbox", "clearco", "novo", "relay financial", "found", "lili", "rho",
    "settle", "routable", "tesorio", "ramp", "navan", "airbase", "brex", "mesh payments",
    "sardine", "sentilink", "socure", "persona", "middesk", "alloy", "unit21", "hummingbird",
    "truework", "argyle", "pinwheel", "method financial", "spade", "quiltt", "moov", "dwolla",
    "astra", "sila", "treasury prime", "synctera", "bond financial", "increase", "highnote",
    "stripe", "checkr", "plaid", "teller", "prime trust", "zeta", "galileo financial",
    "chime", "sofi", "current", "dave", "varo", "one finance", "greenlight", "step", "copper",
    "public", "titan", "wealthfront", "betterment", "altruist", "carta", "addepar", "vise",
    "brightwave", "arta finance", "farther", "savvy wealth", "range", "compound planning",
    "kalshi", "polymarket", "dRW", "hudson river trading", "chronosphere",
    # Insurance / financial protection carriers & platforms (H1B sponsors)
    "prudential financial", "metlife", "new york life", "aig", "the hartford", "travelers",
    "chubb", "allstate", "progressive", "liberty mutual", "nationwide", "mass mutual",
    "guardian life", "northwestern mutual", "aflac", "aflac", "usaa", "cna insurance",
    "corebridge financial", "equitable holdings", "brighthouse financial", "lincoln financial",
    # Crypto-finance infra
    "consensys", "blockchain.com", "okx", "crypto.com", "bitpay", "chainlink labs", "talos",
    "wintermute", "trm labs", "elliptic", "fireblocks", "blockdaemon", "gemini", "ledger",
    "matter labs", "offchain labs", "eigen labs", "figure", "aptos labs", "mysten labs",
]


async def main(apply: bool) -> None:
    data = json.loads(_FILE.read_text())
    existing = {c.strip().lower() for c in data["companies"]}
    cand = [c for c in dict.fromkeys(_CANDIDATES) if c.strip().lower() not in existing]
    print(f"existing sponsors: {len(existing)} | finance candidates to probe: {len(cand)}")

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

    print(f"\nLIVE US boards found: {len(live)}")
    for n, info in live:
        print(f"  + {n:28.28} {info['ats']:14} US={info['us']:>3}/{info['total']}")

    no_board = [n for n, info in results if not info]
    print(f"\nno reachable board (skipped): {len(no_board)}")
    print("  " + ", ".join(no_board))

    if apply and live:
        data["companies"].extend(n for n, _ in live)
        # order-preserving dedup
        seen, deduped = set(), []
        for c in data["companies"]:
            k = c.strip().lower()
            if k not in seen:
                seen.add(k); deduped.append(c)
        data["companies"] = deduped
        _FILE.write_text(json.dumps(data, indent=2))
        print(f"\nAPPLIED — added {len(live)} finance sponsors. board_sponsors now {len(deduped)}.")
    elif not apply:
        print("\nDRY RUN — pass --apply to add the live boards.")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
