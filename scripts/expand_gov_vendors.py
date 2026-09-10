#!/usr/bin/env python3
"""Build/refresh the STATE-GOV C2C vendor universe (data/gov_vendors.json).

Public-sector staff augmentation is where the C2C volume lives: a vendor holds a master contract
with a state agency and posts each requirement on its own public ATS board. This script probes a
candidate vendor list against the board APIs and keeps only vendors with a LIVE board carrying real
US IT contract roles.

Two things it does that `expand_sponsors.py` does not, both learned the hard way:

  * EXACT-SLUG probing. `_fetch_company_postings()` also tries the name's FIRST WORD, which silently
    resolves "harvey nash" (staffing) to "harvey" (Harvey AI) and "apex systems" to "apex" — 300+
    product-company roles entering the queue as staffing. Here every slug is derived from the FULL
    name and probed per-ATS, so a hit is the vendor's own board or nothing.
  * CONTRACT VERIFICATION. A board only counts when it actually carries US IT roles, and the vendor
    is flagged `contract_verified` when the ATS's structured employment flag says Contract on at
    least one — the signal that this is a staffing desk, not a product company's careers page.

Usage:
  python scripts/expand_gov_vendors.py                 # probe candidates, write verified vendors
  python scripts/expand_gov_vendors.py --dry-run       # report only
  python scripts/expand_gov_vendors.py --names "acme corp,foo inc"   # probe extra candidates
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

_ROOT = Path(__file__).parent.parent
_FILE = _ROOT / "data" / "gov_vendors.json"

# US IT staffing / consulting firms that place consultants on STATE & LOCAL government programs
# (DOT, Medicaid/MMIS, revenue, health, DMV) — the corp-to-corp desk. Federal-only primes are
# deliberately absent: their reqs carry clearance / Public Trust bars a visa candidate can't meet.
CANDIDATES = [
    # state/local staff-aug specialists
    "22nd century technologies", "tsc ti", "trigyn technologies", "computer aid inc", "serigor",
    "tekwissen", "vdart", "kyra solutions", "zolon tech", "acro service corporation",
    "knowledge services", "nam info", "softpath system", "kani solutions", "progilytics",
    "prutech solutions", "zillion technologies", "intellyk", "tek fortune", "generis tek",
    "ask consulting", "datasoft technologies", "knowledge builders", "amerit consulting",
    "systematix", "prodware solutions", "ivy tech solutions", "infojini consulting",
    "object technology solutions", "sierra cedar", "walker healthforce", "metasys technologies",
    "smart it frame", "kmm technologies", "w3global", "astir it solutions", "global alliant",
    "business integra", "data systems analysts", "oloop technology solutions",
    "onward technologies", "synergy america", "tech digital", "vertical move", "vision it",
    # broad IT staffing desks that also run public-sector programs
    "technogen", "tanisha systems", "spar information systems", "infinite computing systems",
    "mastech digital", "iris software", "stellent it", "softworld", "lancesoft", "pinnacle group",
    "saicon consultants", "saviance technologies", "shiftcode analytics", "software people",
    "spectraforce technologies", "strategic staffing solutions", "sunrise systems inc",
    "talent burst", "tential solutions", "trident consulting", "triunity software",
    "usm business systems", "v soft consulting", "vlink", "xoriant", "yash technologies",
    "yochana it", "ampcus", "aptask", "artech information systems", "compunnel software",
    "collabera", "computech corporation", "cygnus professionals", "diverse lynx", "eteam",
    "empower professionals", "enterprise solutions inc", "first tek", "hire genics",
    "infovision", "intelliswift software", "kpg99", "mindlance", "miracle software systems",
    "net2source", "nityo infotech", "pyramid consulting", "quantum world technologies",
    "radiansys", "ricefw technologies", "russell tobin", "techgene solutions", "tekgence",
]

# EXACT ATS slugs harvested from the web (WebSearch over jobs.smartrecruiters.com et al) — the
# slug IS the identity here, so no name-to-slug guessing is involved at all. These are the C2C /
# public-sector desks whose slugs don't fall out of their trading name.
SLUG_CANDIDATES = [
    # IT staffing / C2C desks
    "PyramidIT1", "VTS3Corp", "StrategicStaffingSolutionsS3", "TRGInc", "IndotronixInternationalCorp1",
    "360ITProfessionals1", "IPSTechnologyServices", "MITRTechnologies", "INTTechnologies", "ScoutET",
    "AskITConsulting", "StemXpert1", "SouthTekResources", "PartneredStaffing-KellyServices",
    "VTechSolution1", "prosidianconsulting", "RESPECInc", "BCforward3", "USCorp2Corp",
    "systemCanadaTechnologies",
    # Government EMPLOYERS posting their own IT roles on a public board (not vendors — direct).
    "CityOfNewYork",
]

# Board APIs, probed with the EXACT slug (never a first-word guess).
_APIS = {
    "greenhouse":     "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever":          "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby":          "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100",
    "workable":       "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
}
_IT = re.compile(r"\b(engineer|developer|architect|analyst|programmer|devops|sre|scientist|"
                 r"administrator|consultant|data|cloud|java|python|etl|api|software|qa|tester)\b", re.I)


def _slugs(name: str) -> list[str]:
    """Slug candidates from the FULL name only — never the first word (that's the collision path)."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return list(dict.fromkeys([base, base.replace("-", "")]))


def _postings(ats: str, payload) -> list[dict]:
    """Normalize just what verification needs: title, location, employment_type."""
    if ats == "greenhouse":
        return [{"title": p.get("title"), "location": (p.get("location") or {}).get("name", ""),
                 "et": ""} for p in (payload or {}).get("jobs", [])]
    if ats == "lever":
        return [{"title": p.get("text"), "location": (p.get("categories") or {}).get("location", ""),
                 "et": (p.get("categories") or {}).get("commitment", "")} for p in (payload or [])]
    if ats == "ashby":
        return [{"title": p.get("title"), "location": p.get("location", ""),
                 "et": p.get("employmentType", "")} for p in (payload or {}).get("jobs", [])]
    if ats == "smartrecruiters":
        out = []
        for p in (payload or {}).get("content", []):
            loc = p.get("location") or {}
            out.append({"title": p.get("name"),
                        "location": " ".join(str(x) for x in (loc.get("city"), loc.get("region"),
                                                              loc.get("country")) if x),
                        "et": (p.get("typeOfEmployment") or {}).get("label", "")})
        return out
    if ats == "workable":
        return [{"title": p.get("title"), "location": str(p.get("location") or ""),
                 "et": p.get("employment_type", "")} for p in (payload or {}).get("jobs", [])]
    return []


async def _probe(client: httpx.AsyncClient, name: str, exact: str = "") -> dict | None:
    """Probe one candidate. `exact` = a known slug (used verbatim); otherwise slugs are derived
    from the full name."""
    from agents.discovery.apply_url_resolver import loc_is_us_ok
    for slug in ([exact] if exact else _slugs(name)):
        for ats, tmpl in _APIS.items():
            try:
                r = await client.get(tmpl.format(slug=slug), timeout=12)
                if r.status_code != 200:
                    continue
                posts = _postings(ats, r.json())
            except Exception:
                continue
            if not posts:
                continue
            us = [p for p in posts if loc_is_us_ok(p.get("location", ""))]
            it = [p for p in us if _IT.search(p.get("title") or "")]
            if not it:
                continue     # a live board with no US IT roles is not worth a scan slot
            return {"name": name, "slug": slug, "ats": ats, "postings": len(posts),
                    "us": len(us), "it": len(it),
                    "contract_verified": any("contract" in (p.get("et") or "").lower() or
                                             "temporary" in (p.get("et") or "").lower() for p in us),
                    "sample": [p["title"] for p in it[:4]]}
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--names", default="", help="extra comma-separated candidate names")
    ap.add_argument("--slugs", default="", help="extra comma-separated EXACT ATS slugs")
    args = ap.parse_args()

    names = list(dict.fromkeys(CANDIDATES + [n.strip() for n in args.names.split(",") if n.strip()]))
    slugs = list(dict.fromkeys(SLUG_CANDIDATES + [s.strip() for s in args.slugs.split(",") if s.strip()]))
    cands = [(n, "") for n in names] + [(s, s) for s in slugs]
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36"}) as client:
        async def one(name, exact):
            async with sem:
                try:
                    return await _probe(client, name, exact)
                except Exception:
                    return None
        found = [f for f in await asyncio.gather(*[one(n, e) for n, e in cands]) if f]

    found.sort(key=lambda f: (not f["contract_verified"], -f["it"]))
    print(f"live boards: {len(found)}/{len(cands)}")
    for f in found:
        flag = "C2C" if f["contract_verified"] else "   "
        print(f"  [{flag}] {f['name'][:32]:34s} slug={f['slug'][:26]:28s} {f['ats'][:14]:15s} "
              f"us={f['us']:3d} it={f['it']:3d}  e.g. {f['sample'][0][:44] if f['sample'] else ''}")

    if args.dry_run:
        return 0
    prev = json.loads(_FILE.read_text()) if _FILE.exists() else {}
    by_slug = {v["slug"]: v for v in prev.get("vendors", [])}
    for f in found:
        # Refresh the live counts but KEEP the curated display name + kind (vendor vs government
        # employer) — those are hand-set and a probe has no way to re-derive them.
        curated = by_slug.get(f["slug"], {})
        by_slug[f["slug"]] = {**f, "name": curated.get("name", f["name"]),
                              "kind": curated.get("kind", "vendor")}
    _FILE.write_text(json.dumps({
        "_comment": "State/local-government IT staff-aug vendors with a LIVE public ATS board. "
                    "Slugs are exact (never first-word guesses) — see scripts/expand_gov_vendors.py.",
        "vendors": sorted(by_slug.values(), key=lambda v: v["slug"]),
    }, indent=1) + "\n")
    print(f"\nwrote {len(by_slug)} vendors -> {_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
