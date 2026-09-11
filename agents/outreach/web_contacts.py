"""Find vendors' PUBLISHED résumé inboxes on their own websites.

Two problems this solves at once.

Reach: the inbox yields one address per recruiter, and staffing turnover kills those fast — 218 of
584 lifetime sends bounced (37%). A firm's `careers@` / `jobs@` inbox outlives any individual
recruiter, so a dead personal address converts into a live company one.

Trust: the domains are not guessed. Every one is taken from mail the vendor actually sent, so
"ask.com" can never stand in for ASK IT Consulting and "spar.com" can never stand in for SPAR
Information Systems — the collision that poisoned the first attempt at this, and the same shape as
`harvey nash` resolving to Harvey AI. A proven domain removes the whole class of error.

Only addresses the firm PUBLISHES on its own contact or careers page are collected. Those exist to
receive résumés, which is exactly what gets sent to them.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

from core.logger import get_logger

logger = get_logger("web_contacts")

_ROOT = Path(__file__).parent.parent.parent
_OUT = _ROOT / "data" / "web_contacts.json"
_PAST = _ROOT / "data" / "past_vendors.json"

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
# Ordered by how likely the page is to carry a résumé inbox; the scan stops early once one is found.
_PATHS = ("/careers", "/contact", "/contact-us", "", "/join-us", "/joinus", "/about-us", "/jobs")

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_JUNK = re.compile(r"(sentry|\.png|\.jpg|\.gif|example\.|wixpress|godaddy|@2x|webmaster|"
                   r"postmaster|abuse@|dmarc|noreply|no-reply|privacy@|legal@|unsubscribe)", re.I)
# A role inbox is the target: it is published to receive applications and outlives any recruiter.
_ROLE_INBOX = re.compile(r"^(careers?|jobs?|resume[s]?|hr|recruit(ing|ment)?|staffing|talent|"
                         r"apply|hiring|bench|consultants?)@", re.I)
# General inboxes are worth far less — a résumé to sales@ is noise to them and to us.
_LOW_VALUE = re.compile(r"^(sales|marketing|billing|accounts|finance|admin|support|help)@", re.I)


def vendor_domains() -> list[str]:
    """Every distinct vendor domain that has mailed Rahul — proven real, no guessing."""
    from agents.outreach.vendor_harvest import _ROBOT, _FREEMAIL, _NEVER_MAIL
    doms: set[str] = set()
    try:
        past = json.loads(_PAST.read_text())
    except Exception:
        return []
    for v in past:
        email = (v.get("email") or "").lower()
        if "@" not in email or _ROBOT.search(email) or _NEVER_MAIL.search(email):
            continue
        dom = email.split("@")[-1]
        if dom in _FREEMAIL or dom.endswith((".gov", ".edu")):
            continue
        doms.add(dom)
    return sorted(doms)


async def _probe(client: httpx.AsyncClient, domain: str) -> dict | None:
    found: set[str] = set()
    for path in _PATHS:
        for scheme in ("https", "http"):
            try:
                r = await client.get(f"{scheme}://{domain}{path}", timeout=10)
            except Exception:
                if scheme == "http":
                    return None            # domain unreachable on both schemes
                continue
            if r.status_code != 200:
                break
            # Only addresses AT this domain: a page often lists partners and job-board links too.
            found |= {e.lower() for e in _EMAIL.findall(r.text)
                      if not _JUNK.search(e) and e.lower().endswith("@" + domain)}
            break
        if any(_ROLE_INBOX.match(e.split("@")[0] + "@") for e in found):
            break                          # got what we came for; stop hitting their site
    if not found:
        return None
    role = sorted(e for e in found if _ROLE_INBOX.match(e))
    other = sorted(e for e in found if not _ROLE_INBOX.match(e) and not _LOW_VALUE.match(e))
    return {"domain": domain, "role_inboxes": role[:2], "other": other[:2]}


async def harvest(*, limit: int = 0, concurrency: int = 10) -> dict:
    """Probe vendor domains for published résumé inboxes. Writes data/web_contacts.json."""
    domains = vendor_domains()
    if limit:
        domains = domains[:limit]
    async with httpx.AsyncClient(follow_redirects=True, headers=_UA, verify=False) as client:
        sem = asyncio.Semaphore(concurrency)

        async def one(d):
            async with sem:
                try:
                    return await _probe(client, d)
                except Exception:
                    return None

        results = [r for r in await asyncio.gather(*[one(d) for d in domains]) if r]

    with_role = [r for r in results if r["role_inboxes"]]
    payload = {
        "_comment": "Résumé inboxes published on vendors' own careers/contact pages. Domains are "
                    "taken from mail the vendor sent, never guessed. Built by "
                    "agents/outreach/web_contacts.harvest().",
        "domains_probed": len(domains), "vendors": results,
    }
    _OUT.write_text(json.dumps(payload, indent=1) + "\n")
    logger.info("web_contacts_harvested", probed=len(domains), found=len(results),
                with_role_inbox=len(with_role))
    return payload


def merge_into_outreach() -> dict:
    """Add the discovered role inboxes to the outreach pool, flagged first_contact."""
    from agents.outreach.vendor_harvest import _OUT as POOL
    from agents.outreach.deliverability import load_bounced
    try:
        found = json.loads(_OUT.read_text()).get("vendors", [])
        payload = json.loads(POOL.read_text())
    except Exception:
        return {"added": 0}
    bounced = load_bounced()
    by_email = {c["email"]: c for c in payload.get("contacts", [])}
    added = 0
    for v in found:
        # Role inboxes only. A general info@/contact@ address gets a résumé filed as noise, and
        # mailing many of them is what makes a sender look like a list buyer.
        for email in v["role_inboxes"]:
            if email in by_email or email in bounced:
                continue
            by_email[email] = {
                "email": email, "domain": v["domain"], "name": "", "reqs": 1,
                "gov_reqs": 0, "weak_reqs": 0, "states": [], "subjects": [],
                "source": "web:careers_page", "first_contact": True, "already_contacted": False,
            }
            added += 1
    payload["contacts"] = sorted(by_email.values(),
                                 key=lambda c: (-c.get("gov_reqs", 0), -c.get("reqs", 0)))
    POOL.write_text(json.dumps(payload, indent=1) + "\n")
    logger.info("web_contacts_merged", added=added)
    return {"added": added, "pool_total": len(by_email)}


# ── Discovery from job boards ─────────────────────────────────────────────────────────────────
# Product companies posting their own roles are not vendors; only staffing firms hold a bench.
_STAFFING_NAME = re.compile(
    r"\b(staffing|consult(ing|ants?)|technolog(y|ies)|solutions?|systems?|services|group|"
    r"partners?|resourc(es|ing)|infotech|softech|soft|labs|associates|recruit\w*|talent|"
    r"global|international|enterprises?|corp|inc|llc)\b", re.I)
_NOT_VENDOR = re.compile(
    r"^(jpmorgan|citi|ibm|accenture|snowflake|google|amazon|microsoft|meta|apple|oracle|"
    r"salesforce|deloitte|pwc|kpmg|ey |capital one|walmart|target|nike|disney|saic|leidos|"
    r"booz|northrop|lockheed|raytheon|general dynamics)", re.I)


def _name_domains(name: str) -> list[str]:
    """Domain candidates from the FULL name only — never a fragment. A first-word guess maps
    'ASK IT Consulting' to ask.com and 'SPAR Information Systems' to spar.com."""
    base = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    base = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|the|group)\b", " ", base)
    joined = re.sub(r"\s+", "", base)
    return [d for d in dict.fromkeys([f"{joined}.com", f"{joined}inc.com", f"{joined}.net"])
            if 9 < len(d) < 40]


def is_vendor(name: str) -> bool:
    return bool(_STAFFING_NAME.search(name)) and not _NOT_VENDOR.match(name.strip())


async def harvest_names(names: list[str], *, concurrency: int = 10) -> list[dict]:
    """Resolve staffing-firm NAMES (e.g. harvested from a job board) to published résumé inboxes.

    Unlike vendor_domains(), the domain here is a guess, so each hit must be VERIFIED: the page has
    to actually contain a distinctive word from the firm's name. Without that check the guesses
    silently resolve to unrelated companies.
    """
    targets = [n for n in names if is_vendor(n)]
    async with httpx.AsyncClient(follow_redirects=True, headers=_UA, verify=False) as client:
        sem = asyncio.Semaphore(concurrency)

        async def one(name):
            tokens = [t for t in re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
                      if len(t) > 4 and t not in ("group", "technologies", "solutions", "systems",
                                                  "consulting", "services", "staffing", "global")]
            async with sem:
                for dom in _name_domains(name):
                    try:
                        got = await _probe(client, dom)
                    except Exception:
                        continue
                    if not got or not got["role_inboxes"]:
                        continue
                    try:
                        r = await client.get(f"https://{dom}", timeout=10)
                        body = r.text.lower()
                    except Exception:
                        continue
                    if tokens and not any(t in body for t in tokens):
                        continue          # domain belongs to someone else
                    return {**got, "vendor": name}
            return None

        return [r for r in await asyncio.gather(*[one(n) for n in targets]) if r]
