"""
Resolve careers-LISTING URLs → the real per-job APPLICATION-form URL.

The careers-page scrape strategy (job_scanner._try_playwright_scrape) often can't find a
per-job apply link and falls back to `{website}/careers` — a listing/index page with no
form. The submit lane then finds no submit button (Pinecone/Arize/Fireworks all hit this).

This module opens the listing page, reads the real anchor links rendered on it, and maps
each job to its own application URL by matching the link text to the job title (and
rewarding hrefs that look like an ATS per-job route). Used two ways:
  • backfill_listing_urls(state, ...): repair apply_urls already sitting in the parking lot.
  • resolve_for_titles(...): called by the scraper so NEW jobs capture real URLs up front.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from core.logger import get_logger

logger = get_logger("apply_url_resolver")

# A URL that is almost certainly a listing/index page, not a single job's form.
_LISTING_TAILS = ("/careers", "/careers/", "/jobs", "/jobs/", "/work-with-us",
                  "/about/careers", "/company/careers", "/open-positions", "/openings")
# href fragments that signal a real per-job application route.
_JOB_HREF_HINTS = ("greenhouse.io", "gh_jid", "lever.co", "ashbyhq.com", "myworkdayjobs",
                   "smartrecruiters.com", "icims.com", "/job/", "/jobs/", "/job-", "/position",
                   "/opening/", "/openings/", "/apply", "/o/", "/p/", "gh_src", "jobId=",
                   "/careers/job", "greenhouse", "lever", "ashby")
# Generic nav/footer link text that is never a specific job row.
_NAV_TEXT = {"careers", "jobs", "open positions", "view all", "see all jobs", "all jobs",
             "home", "about", "about us", "contact", "contact us", "login", "log in",
             "sign in", "apply", "apply now", "back", "next", "previous", "blog", "press",
             "privacy", "terms", "life at", "our team", "benefits", "culture", "search"}
_TITLE_STOP = {"the", "and", "of", "for", "a", "an", "to", "at", "in", "ii", "iii", "iv",
               "sr", "jr", "senior", "staff", "principal", "lead", "i", "remote", "hybrid"}


# A URL that is already a single job's application form on a known ATS — never re-resolve
# or follow an "Apply" hop from these (doing so can wander off to a marketing page).
_TERMINAL_FORM_SIGNALS = ("gh_jid", "greenhouse.io/", "jobs.lever.co/", "ashbyhq.com/",
                          "myworkdayjobs.com", "smartrecruiters.com/", "icims.com/",
                          "jobvite.com/", "workable.com/", "/application", "jobid=")


def is_terminal_form_url(url: str) -> bool:
    """True if `url` is already a per-job application form on a recognized ATS."""
    u = (url or "").lower()
    return any(sig in u for sig in _TERMINAL_FORM_SIGNALS)


def is_listing_url(url: str) -> bool:
    """True if `url` looks like a careers index, not a single-job application page."""
    if not url:
        return True
    u = url.strip().lower().rstrip("/")
    path = urlparse(u).path
    # A bare domain or a known listing tail with no per-job id/slug after it.
    if not path or path == "":
        return True
    if any(u.endswith(t.rstrip("/")) for t in _LISTING_TAILS):
        return True
    # Has an ATS per-job signal → it's a real form, not a listing.
    if any(h in u for h in ("gh_jid", "jobid=", "lever.co/", "ashbyhq.com", "myworkdayjobs")):
        return False
    return False


def _title_tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in _TITLE_STOP and len(w) > 2}


def _score_anchor(href: str, text: str, title_toks: set, base_url: str) -> float:
    """Higher = more likely THIS anchor is the application link for the target job."""
    if not href:
        return 0.0
    h = href.lower()
    t = (text or "").strip().lower()
    if not t or t in _NAV_TEXT or len(t) < 4:
        # No usable text — only trust it if the href is an unmistakable per-job route.
        return 1.0 if any(x in h for x in ("gh_jid", "jobid=", "/job/", "lever.co/")) else 0.0
    # Same page / pure anchor / mailto → useless.
    if h.startswith(("mailto:", "tel:", "javascript:", "#")):
        return 0.0
    abs_href = urljoin(base_url, href).rstrip("/").lower()
    if abs_href == base_url.rstrip("/").lower():
        return 0.0
    overlap = len(_title_tokens(text) & title_toks)
    score = float(overlap)                       # title-match is the main signal
    if any(hint in h for hint in _JOB_HREF_HINTS):
        score += 1.5                             # looks like a per-job route
    if overlap == 0:
        score *= 0.0 if "gh_jid" not in h else 0.5  # don't grab the wrong job
    return score


async def _collect_anchors(page) -> list[tuple[str, str]]:
    """Return [(href, text)] for every anchor on the page (all frames)."""
    anchors: list[tuple[str, str]] = []
    frames = [page, *(getattr(page, "frames", []) or [])]
    for fr in frames:
        try:
            data = await fr.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => [e.getAttribute('href') || '', (e.innerText||e.textContent||'').trim()])",
            )
            anchors.extend((h, t) for h, t in data if h)
        except Exception:
            continue
    return anchors


def _best_match(anchors, title: str, base_url: str) -> str | None:
    title_toks = _title_tokens(title)
    if not title_toks:
        return None
    best, best_score = None, 0.0
    for href, text in anchors:
        s = _score_anchor(href, text, title_toks, base_url)
        if s > best_score:
            best, best_score = href, s
    # Require a real signal: at least one title token matched (score>=1) AND not the page itself.
    if best and best_score >= 1.0:
        return urljoin(base_url, best)
    return None


_APPLY_TEXT = ("apply", "apply now", "apply for this", "apply to this", "submit application",
               "apply here", "start application")


async def _has_real_form(page) -> bool:
    """True if the page already shows a fillable application form (not just a search box)."""
    try:
        for fr in [page, *(getattr(page, "frames", []) or [])]:
            try:
                n = await fr.eval_on_selector_all(
                    "input, textarea, select",
                    "els => els.filter(e => {const t=(e.type||'').toLowerCase();"
                    "return !['search','hidden','submit','button'].includes(t);}).length")
                if isinstance(n, int) and n >= 3:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


async def _find_apply_link(page, base_url: str) -> str | None:
    """On a job-DETAIL page, find the 'Apply' link that leads to the actual form."""
    anchors = await _collect_anchors(page)
    best, best_score = None, 0.0
    for href, text in anchors:
        if not href or href.lower().startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        t = (text or "").strip().lower()
        h = href.lower()
        score = 0.0
        if any(t == a or t.startswith(a) for a in _APPLY_TEXT):
            score += 2.0
        if any(hint in h for hint in ("greenhouse", "lever.co", "ashbyhq", "myworkdayjobs",
                                      "gh_jid", "/apply", "smartrecruiters", "jobvite",
                                      "icims", "workable", "/application")):
            score += 1.5
        abs_h = urljoin(base_url, href).rstrip("/").lower()
        if abs_h == base_url.rstrip("/").lower():
            continue
        if score > best_score:
            best, best_score = urljoin(base_url, href), score
    return best if best_score >= 1.5 else None


def _kw(s: str) -> set:
    # tokens for job-title matching — KEEP location words (US/EMEA/remote) so we don't
    # match the EMEA twin of a US role.
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 1}


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "").replace("&amp;", "&").replace("&#39;", "'")


# Process-level cache: norm(company) → list[{ats,title,url,description}] or None. A company
# lives on ONE ATS, so we fetch its whole board ONCE and match every title locally. 13 Mistral
# leads → one Lever call, not 13 × 12 HTTP requests.
# TTL'd: stores (fetched_at, result). A long-running scheduler MUST re-fetch periodically or it
# would never see newly-posted roles (the early-applicant edge). 20-min TTL balances freshness vs
# load — short enough to catch new postings within a scan cycle, long enough to dedup a single run.
_COMPANY_POSTINGS: dict[str, tuple[float, list | None]] = {}
_POSTINGS_TTL = 1200.0
# Hard ceiling on SmartRecruiters pagination — a few staffing desks list thousands of reqs and we
# only need enough depth to cover the fresh, in-lane ones.
_SR_MAX_POSTINGS = 600


async def _fetch_company_postings(company: str) -> list | None:
    """Fetch + normalize a company's whole board from its ATS (Greenhouse/Lever/Ashby/Workable).
    Queries the 4 APIs in PARALLEL per slug, 6s timeout, first non-empty wins. Cached with a TTL so
    a long-running process re-fetches and picks up fresh postings."""
    import asyncio
    import time
    import httpx
    from agents.discovery.job_scanner import _slug_variants, GREENHOUSE_API, LEVER_API, ASHBY_API

    key = re.sub(r"\s+", " ", (company or "").strip().lower())
    cached = _COMPANY_POSTINGS.get(key)
    if cached and (time.time() - cached[0]) < _POSTINGS_TTL:
        return cached[1]

    def _gh(j, slug=None):
        # Use the Greenhouse EMBED application form (boards.greenhouse.io/embed/job_app?for={slug}&
        # token={id}) — the bare, always-live apply form. The canonical hosted page
        # (job-boards.greenhouse.io/{slug}/jobs/{id}) 302-REDIRECTS to the company's branded careers
        # SPA for many employers (e.g. Stripe → stripe.com/jobs/search?gh_jid=…, a LISTING, not the
        # form). The embed endpoint never redirects off-site, so it's the reliable apply target.
        def _embed_from_url(u):  # canonical job-boards.greenhouse.io/{slug}/jobs/{id} → embed form
            m = re.search(r"greenhouse\.io/([a-z0-9_.-]+)/jobs/(\d+)", u or "", re.I)
            return f"https://boards.greenhouse.io/embed/job_app?for={m.group(1)}&token={m.group(2)}" if m else None
        out = []
        for p in (j or {}).get("jobs", []):
            jid = p.get("id")
            # ALWAYS land on the embed form. When slug isn't passed, derive it from the absolute_url
            # (slug+id are in it) instead of falling back to the canonical URL, which 302s to a branded
            # SPA / 406s — the leak that put 169 canonical URLs in the queue.
            url = ((f"https://boards.greenhouse.io/embed/job_app?for={slug}&token={jid}" if slug and jid else None)
                   or _embed_from_url(p.get("absolute_url"))
                   or p.get("absolute_url"))
            # Greenhouse has no reliable structured employment-type field — a metadata entry named
            # like "Employment Type"/"Job Type" is the best we get (else "" → fall back to JD/source).
            _gh_et = next((str(m.get("value")) for m in (p.get("metadata") or [])
                           if "employ" in (m.get("name") or "").lower()
                           or "job type" in (m.get("name") or "").lower()), "")
            out.append({"ats": "greenhouse", "title": p.get("title"), "url": url,
                        "location": (p.get("location") or {}).get("name", ""),
                        # Use the MORE RECENT of first_published/updated_at: many open Greenhouse reqs
                        # were first posted long ago but are actively maintained (recent updated_at).
                        # Keying freshness off first_published alone flags ~84% of OPEN roles "stale".
                        "posted_at": _newer_iso(p.get("updated_at"), p.get("first_published")),
                        "employment_type": _gh_et,
                        "description": _strip_html(p.get("content", ""))})
        return out

    def _lv(j, slug=None):
        out = []
        for p in (j or []):
            parts = [p.get("descriptionPlain") or _strip_html(p.get("description", ""))]
            for s in (p.get("lists") or []):
                parts.append(_strip_html(s.get("content", "")))
            cats = p.get("categories") or {}
            loc = cats.get("location") or ""
            allloc = cats.get("allLocations") or []
            out.append({"ats": "lever", "title": p.get("text"),
                        "url": p.get("applyUrl") or p.get("hostedUrl"),
                        "location": " | ".join([loc] + [x for x in allloc if x != loc]),
                        "posted_at": _epoch_ms_to_iso(p.get("createdAt")),
                        # Lever's STRUCTURED commitment: "Full-time" | "Contract" | "Temporary" | ...
                        "employment_type": cats.get("commitment", ""),
                        "description": "\n".join(x for x in parts if x)})
        return out

    def _ash(j, slug=None):
        out = []
        for p in (j or {}).get("jobs", []):
            secondary = " ".join(s.get("location", "") for s in (p.get("secondaryLocations") or []))
            loc = (p.get("location") or "") + (" " + secondary if secondary else "")
            if p.get("isRemote") and "remote" not in loc.lower():
                loc = (loc + " Remote").strip()
            out.append({"ats": "ashby", "title": p.get("title"),
                        "url": p.get("applyUrl") or p.get("jobUrl"), "location": loc.strip(),
                        "posted_at": p.get("publishedAt") or "",
                        # Ashby STRUCTURED employmentType: "FullTime" | "Contract" | "Temporary" | ...
                        "employment_type": p.get("employmentType", ""),
                        "description": p.get("descriptionPlain") or _strip_html(p.get("descriptionHtml", ""))})
        return out

    WORKABLE = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    SMARTRECRUITERS = "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"

    def _sr(j, slug=None):
        out = []
        for p in (j or {}).get("content", []):
            loc = p.get("location") or {}
            loc_str = " ".join(str(x) for x in (loc.get("city"), loc.get("region"),
                                                loc.get("country")) if x)
            if loc.get("remote") and "remote" not in loc_str.lower():
                loc_str = (loc_str + " Remote").strip()
            pid = p.get("id")
            url = f"https://jobs.smartrecruiters.com/{slug}/{pid}" if pid else None
            out.append({"ats": "smartrecruiters", "title": p.get("name"), "url": url,
                        "location": loc_str, "posted_at": p.get("releasedDate") or "",
                        # SmartRecruiters STRUCTURED typeOfEmployment: {label: "Full-time"|"Contract"}
                        "employment_type": (p.get("typeOfEmployment") or {}).get("label", ""),
                        "description": ""})
        return out

    def _wk(j, slug=None):
        out = []
        for p in (j or {}).get("jobs", []):
            loc = p.get("location") or {}
            loc_str = loc if isinstance(loc, str) else " ".join(
                str(x) for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
            out.append({"ats": "workable", "title": p.get("title"),
                        "url": p.get("application_url") or p.get("url"),
                        "location": loc_str, "posted_at": p.get("published_on") or p.get("created_at") or "",
                        "employment_type": p.get("employment_type") or p.get("type") or "",
                        "description": ""})
        return out

    result = None
    try:
        async with httpx.AsyncClient(timeout=6, headers={"Accept": "application/json"}) as c:
            for slug in _slug_variants(company):
                if not slug:
                    continue
                gh, lv, ah, wk, sr = await asyncio.gather(
                    c.get(GREENHOUSE_API.format(slug=slug)),
                    c.get(LEVER_API.format(slug=slug)),
                    c.get(ASHBY_API.format(slug=slug)),
                    c.get(WORKABLE.format(slug=slug)),
                    c.get(SMARTRECRUITERS.format(slug=slug)),
                    return_exceptions=True,
                )
                for resp, parse in ((gh, _gh), (lv, _lv), (ah, _ash), (wk, _wk), (sr, _sr)):
                    if isinstance(resp, Exception) or getattr(resp, "status_code", 0) != 200:
                        continue
                    try:
                        postings = [p for p in parse(resp.json(), slug) if p.get("title") and p.get("url")]
                    except Exception:
                        postings = []
                    # SmartRecruiters caps a page at 100. Big staffing desks (the whole contract
                    # lane) run 100+ open reqs, so a single page silently hid most of their board —
                    # and every hidden role also failed to RESOLVE. Page through with `offset`.
                    if postings and parse is _sr and len(postings) >= 100:
                        for offset in range(100, _SR_MAX_POSTINGS, 100):
                            try:
                                more = await c.get(SMARTRECRUITERS.format(slug=slug) + f"&offset={offset}")
                                page = [p for p in _sr(more.json(), slug) if p.get("title") and p.get("url")]
                            except Exception:
                                break
                            if not page:
                                break
                            postings.extend(page)
                            if len(page) < 100:
                                break
                    if postings:
                        result = postings
                        break
                if result:
                    break
    except Exception as e:
        logger.warning("company_postings_fetch_failed", company=company, error=str(e)[:80])

    _COMPANY_POSTINGS[key] = (time.time(), result)
    return result


# US vs foreign location classification — Rahul is US-based and needs US sponsorship, so we must
# never queue (or resolve to) a foreign-country variant of a role.
_FOREIGN_LOC = (
    "france", "paris", "marseille", "lyon", "sophia antipolis", "germany", "munich", "münchen",
    "berlin", "hamburg", "united kingdom", "uk", "london", "manchester", "ireland",
    "dublin", "singapore", "australia", "sydney", "melbourne", "netherlands", "amsterdam",
    "sweden", "stockholm", "spain", "madrid", "barcelona", "canada", "toronto", "vancouver",
    "montreal", "india", "bangalore", "bengaluru", "hyderabad", "pune", "gurgaon", "tokyo",
    "japan", "poland", "warsaw", "krakow", "kraków", "portugal", "lisbon", "switzerland", "zurich",
    "zürich", "emea", "apac", "latam", "brazil", "sao paulo", "são paulo", "mexico", "israel",
    "tel aviv", "dubai", "uae", "abu dhabi", "ontario", "quebec", "québec", "korea", "seoul",
    "china", "shanghai", "beijing", "hong kong", "taiwan", "philippines", "manila", "vietnam",
    "indonesia", "jakarta", "italy", "milan", "rome", "denmark", "copenhagen", "norway", "oslo",
    "finland", "helsinki", "austria", "vienna", "belgium", "brussels", "romania", "bucharest",
    "lausanne", "geneva", "luxembourg", "belarus", "minsk", "ukraine", "kyiv", "estonia", "tallinn",
    "iceland", "reykjavik", "reykjavík", "norway", "bergen", "iceland", "scotland", "edinburgh",
    "wales", "cardiff", "cyprus", "malta", "croatia", "zagreb", "slovenia", "ljubljana",
    "czech", "prague", "greece", "athens", "turkey", "türkiye", "turkiye", "istanbul", "ankara",
    "south africa", "nigeria", "lagos", "kenya", "nairobi", "egypt", "cairo", "morocco",
    "argentina", "buenos aires", "colombia", "bogota", "bogotá", "chile", "santiago", "peru",
    "lima", "venezuela", "caracas", "ecuador", "uruguay", "costa rica", "panama", "guatemala",
    "latin america", "latam", "south america", "central america", "middle east", "mena",
    "europe", "european union", "eu remote", "asia pacific", "asia-pacific", "oceania",
    "pakistan", "karachi", "lahore", "bangladesh", "dhaka", "sri lanka", "colombo", "nepal",
    "thailand", "bangkok", "malaysia", "kuala lumpur", "saudi", "riyadh", "qatar", "doha",
    "hungary", "budapest", "bulgaria", "sofia", "serbia", "belgrade", "lithuania", "vilnius",
    "latvia", "riga", "slovakia", "bratislava", "armenia", "yerevan", "georgia (country)",
    "new zealand", "auckland", "wellington", "bahrain", "kuwait", "oman", "jordan", "amman",
    # India tech hubs — 'gurgaon' was renamed 'gurugram' (2016); most of these were missing,
    # so India-located roles leaked through as 'unknown'. Chosen to avoid US-namesake collisions.
    "gurugram", "noida", "new delhi", "mumbai", "navi mumbai", "chennai", "kolkata", "ahmedabad",
    "chandigarh", "coimbatore", "kochi", "mysuru", "nagpur", "indore", "vadodara", "visakhapatnam",
    "bhubaneswar", "gandhinagar", "thiruvananthapuram", "jaipur", "kanpur", "lucknow",
    # other clearly-foreign hubs frequently seen on tech boards (country names + unambiguous cities)
    "bangkok", "thailand", "kuala lumpur", "malaysia", "taipei", "osaka", "frankfurt", "stuttgart",
    "rotterdam", "eindhoven", "gothenburg", "wroclaw", "wrocław", "gdansk", "gdańsk", "budapest",
    "hungary", "sofia", "bulgaria", "belgrade", "serbia", "tallinn", "riga", "vilnius", "bratislava",
    "brno", "ottawa", "calgary", "guadalajara", "monterrey", "bogota", "bogotá", "buenos aires",
    "cairo", "egypt", "nairobi", "lagos", "riyadh", "doha", "amman", "casablanca",
)
# STRONG US signals: country + spelled-out states. Unambiguously US even when a foreign token is
# also present (multi-loc "San Jose; London, UK" → US-available). NO foreign-city collisions here.
_US_STRONG_LOC = (
    "united states", "usa", "us", "u.s", "americas", "north america", "nationwide",
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas",
    "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
)
# WEAK US signals: city/hub names. These DO collide with foreign namesakes ("Cambridge, UK",
# "Boston, UK"), so a city match only counts as US when NO foreign country is present.
_US_CITY_LOC = (
    "san francisco", "bay area", "nyc", "seattle", "boston", "austin", "denver",
    "chicago", "atlanta", "los angeles", "san jose", "san diego", "dallas", "houston",
    "mountain view", "menlo park", "palo alto", "san mateo", "sunnyvale", "redmond", "bellevue",
    "raleigh", "durham", "charlotte", "columbus", "nashville", "pittsburgh", "minneapolis",
    "salt lake city", "phoenix", "tempe", "scottsdale", "portland", "miami", "philadelphia",
    "washington dc", "washington, d.c", "reston", "herndon", "arlington", "mclean", "tysons",
    "cambridge", "somerville", "ann arbor", "madison", "boulder", "irvine", "santa clara",
    "santa monica", "culver city", "cupertino", "fremont", "oakland", "brooklyn", "manhattan",
    "jersey city", "hoboken", "plano", "irving", "frisco", "richardson", "fort worth",
    "san antonio", "kansas city", "saint louis", "st. louis", "st louis", "cincinnati",
    "cleveland", "indianapolis", "tampa", "orlando", "sacramento", "san bruno", "foster city",
    "south san francisco", "emeryville", "burlingame", "bentonville", "raleigh-durham",
)
_US_LOC = _US_STRONG_LOC + _US_CITY_LOC
# US state abbreviations in the conventional "City, ST" form. Matched case-sensitively on the
# ORIGINAL (un-lowercased) text and only after a comma, so it can't collide with common English
# words ('in', 'or', 'me', 'pa', 'hi' etc.) — those are lowercase and not comma-prefixed here.
_US_STATE_ABBR = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY", "DC",
)
_US_ABBR_RE = re.compile(r",\s*(?:" + "|".join(_US_STATE_ABBR) + r")\b")
# Tokens that, as the ENTIRE residual after stripping remote/office words, still mean US: state
# codes (ca, ny, tx…), big-city/region abbrevs, and US timezones. Used so "Remote - CA" (California)
# and "SF Office Remote" aren't mistaken for foreign. (Canadian 'ON'/'BC' aren't here → correctly
# rejected.) Requiring ALL residual tokens to be in this set avoids the 'in'/'or' word collisions.
_US_TOKENS = {a.lower() for a in _US_STATE_ABBR} | {
    "sf", "sfo", "nyc", "la", "lax", "sea", "bos", "chi", "dfw", "atx", "pdx", "bay", "silicon",
    "valley", "usa", "us", "united", "states", "america", "american", "nationwide", "anywhereus",
    "est", "edt", "pst", "pdt", "cst", "cdt", "mst", "mdt", "et", "pt", "ct",
}
# Word-boundary matching — naive substring matching mis-fires ('usa' inside 'lausanne',
# 'us' inside 'belarus'). \b ensures whole-word matches only.
_US_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in _US_LOC) + r")\b")
_US_STRONG_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in _US_STRONG_LOC) + r")\b")
_FOREIGN_RE = re.compile(r"\b(?:" + "|".join(re.escape(k) for k in _FOREIGN_LOC) + r")\b")


def _newer_iso(*vals) -> str:
    """Return the most recent of several ISO datetime strings ('' if none parse)."""
    best, best_dt = "", None
    for v in vals:
        if not v:
            continue
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if best_dt is None or dt > best_dt:
                best, best_dt = str(v), dt
        except Exception:
            continue
    return best


def _epoch_ms_to_iso(ms) -> str:
    """Lever createdAt is epoch milliseconds → ISO date string ('' if absent/bad)."""
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def posting_age_days(posted_at: str):
    """Age in days of an ISO posted_at string, or None if unknown/unparseable."""
    if not posted_at:
        return None
    try:
        from datetime import datetime, timezone
        s = str(posted_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except Exception:
        return None


def _loc_class(text: str) -> str:
    """'us', 'foreign', or 'unknown' for a location/title string. An explicit US signal wins over
    foreign (a 'US; London' multi-loc role counts as US-available); foreign signal beats a bare
    'remote' (a 'Remote - EMEA' role is foreign); a bare 'remote' at a US-HQ'd company → US.
    The 'City, ST' state-abbreviation pattern (matched on the original case) also counts as US."""
    raw = text or ""
    t = raw.lower()
    # STRONG US signal (", CA" / spelled-out state / "United States") wins even alongside a foreign
    # token — a genuine multi-location US-available role ("San Jose; London, UK").
    if _US_ABBR_RE.search(raw) or _US_STRONG_RE.search(t):
        return "us"
    # A foreign COUNTRY now beats a WEAK US-city match — so "Cambridge, UK" / "Boston, UK" (UK
    # cities that share a name with US hubs) are correctly foreign, not US.
    if _FOREIGN_RE.search(t):
        return "foreign"
    if _US_RE.search(t):
        return "us"
    # "remote" counts as US ONLY when the string is JUST remote-ish words (a US-HQ remote role).
    # If any OTHER place token remains ("Türkiye, Remote", "Venezuela, Remote", "Latin America"),
    # we CANNOT confirm US → 'unknown' (rejected by strict), so a foreign-remote role whose country
    # we don't explicitly list can't leak through as US (the Sezzle/Türkiye/Venezuela bug).
    residual = re.sub(r"[,\-/|()]+", " ", t)
    residual = re.sub(r"\b(remote|wfh|work\s*from\s*home|hybrid|on[\s-]?site|onsite|flexible|"
                      r"telecommute|in[\s-]?office|office|hq|headquarters|based|only|greater|metro|"
                      r"area|region)\b", " ", residual)
    toks = re.findall(r"[a-z]+", residual)
    if not toks:
        return "us" if ("remote" in t or "anywhere" in t) else "unknown"
    # ALL leftover tokens are US state codes / city abbrevs / timezones → US (e.g. "Remote - CA",
    # "SF Office Remote"). Any unrecognized token ("türkiye", "venezuela", "europe") → unknown,
    # which strict mode rejects — so foreign-remote can't leak through.
    if all(w in _US_TOKENS for w in toks):
        return "us"
    return "unknown"


def loc_is_us_ok(text: str, *, strict: bool = True) -> bool:
    """Gate for whether a location may enter the US-only queue.
    strict=True (default): require a POSITIVE US signal — 'unknown' is rejected, so an unlisted
      foreign city can't leak through as 'unknown'. strict=False: legacy lenient (us OR unknown)."""
    c = _loc_class(text)
    return c == "us" if strict else c != "foreign"


def us_first_location(text: str) -> tuple[str, bool]:
    """For a multi-location string that mixes US and foreign offices (e.g. 'Dublin, Ireland; Los
    Angeles, USA'), return (display, is_multi_foreign) where display LEADS with the US office(s) and
    notes the foreign one(s) — so a US-only candidate sees/applies to the US location, not the foreign
    one shown first. Single-location or US-only strings pass through unchanged with False."""
    raw = (text or "").strip()
    parts = [p.strip() for p in re.split(r"\s*[;|\n]\s*|\s+/\s+", raw) if p.strip()]
    if len(parts) <= 1:
        return raw, False
    us = [p for p in parts if _loc_class(p) == "us"]
    foreign = [p for p in parts if _loc_class(p) == "foreign"]
    other = [p for p in parts if _loc_class(p) not in ("us", "foreign")]
    if us and foreign:
        lead = "; ".join(us + other)
        return f"{lead}  (also offered abroad: {'; '.join(foreign)})", True
    return raw, False


async def resolve_ats_for(company: str, title: str, *, us_only: bool = False) -> dict | None:
    """KEYSTONE of 'discover on LinkedIn, apply on the company ATS': company + role title →
    the real per-job application URL + JD, by querying the company's ATS PUBLIC API directly
    (Greenhouse/Lever/Ashby/Workable). ZERO LinkedIn nav, no scraping, no account risk, no
    CAPTCHA on discovery. Returns {url, ats, matched_title, location, location_class, description}.

    us_only=True: STRICT — only ever returns a US-based (or US-available remote) posting. For a
    role posted in several countries it picks the US variant; if no US variant exists, returns
    None. Fast: the whole board is fetched once (parallel, cached) and matched locally."""
    tt = _kw(title)
    if not tt:
        return None
    postings = await _fetch_company_postings(company)
    if not postings:
        return None

    scored = []
    for p in postings:
        score = len(_kw(p["title"]) & tt)
        if score >= 2:
            cls = _loc_class((p.get("location") or "") + " " + (p.get("title") or ""))
            scored.append((score, cls, p))
    if not scored:
        return None

    if us_only:
        # STRICT: require a positive US signal — drop foreign AND unknown-location variants, so an
        # unlisted foreign city can't slip through as 'unknown'.
        scored = [s for s in scored if s[1] == "us"]
        if not scored:
            return None
    # Prefer: highest title score, then a US posting over an unknown-location one.
    scored.sort(key=lambda s: (s[0], 1 if s[1] == "us" else 0), reverse=True)
    _score, cls, best = scored[0]
    return {"url": best["url"], "ats": best["ats"], "matched_title": best["title"],
            "location": best.get("location", ""), "location_class": cls,
            "posted_at": best.get("posted_at", ""),
            "employment_type": best.get("employment_type", ""),  # ATS structured contract flag
            "description": (best.get("description") or "")[:4000]}


async def _resolve_workable(slug: str, title: str) -> str | None:
    """Workable boards (e.g. apply.workable.com/huggingface) render job links via JS, so
    anchors don't expose them. Use the public widget API to match the title → apply URL."""
    import httpx
    api = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(api, timeout=12, headers={"Accept": "application/json"})
            jobs = (r.json() or {}).get("jobs", [])
    except Exception as e:
        logger.warning("workable_api_failed", slug=slug, error=str(e)[:80])
        return None
    tt = _kw(title)
    best, best_score = None, 0
    for j in jobs:
        score = len(_kw(j.get("title", "")) & tt)
        if score > best_score:
            best, best_score = j, score
    if best and best_score >= 2:
        url = best.get("application_url") or best.get("url")
        logger.info("workable_resolved", title=title[:50], match=best.get("title", "")[:50], url=url)
        return url
    return None


async def resolve_one(browser, listing_url: str, title: str, _depth: int = 0) -> str | None:
    """Open `listing_url`, find the anchor matching `title`, return its absolute URL. If that
    lands on a job-DETAIL page (no form, has an 'Apply' link), follow one hop to the form."""
    import asyncio
    # Already a per-job ATS form? Leave it — re-resolving a good form risks matching a
    # stray anchor (e.g. a marketing link) and wandering off the real application.
    if _depth == 0 and is_terminal_form_url(listing_url):
        return None
    try:
        page = await browser.new_page(listing_url)
    except Exception as e:
        logger.warning("resolve_open_failed", url=listing_url, error=str(e)[:80])
        return None
    try:
        await asyncio.sleep(1.5)  # let SPA job lists render
        anchors = await _collect_anchors(page)
        landing = page.url or listing_url
        # Workable boards render job links via JS (anchors are just "#jobs") — resolve via
        # the public API. The careers page often redirects to apply.workable.com/{slug}.
        m = re.search(r"workable\.com/([A-Za-z0-9_-]+)", landing)
        if m and "apply.workable.com" in landing and m.group(1) not in ("j", "api"):
            wk = await _resolve_workable(m.group(1), title)
            if wk:
                return wk
        match = _best_match(anchors, title, landing)
        if match and match.rstrip("/").lower() != landing.rstrip("/").lower():
            # Matched a per-job link. If it's a job-DETAIL page (not yet a terminal ATS
            # form), follow it ONE more hop to reach the actual application form.
            if _depth == 0 and not is_terminal_form_url(match):
                deeper = await resolve_one(browser, match, title, _depth=1)
                if deeper:
                    logger.info("apply_url_resolved", title=title[:50], resolved=deeper)
                    return deeper
            logger.info("apply_url_resolved", title=title[:50], resolved=match)
            return match
        # No per-job link, but maybe THIS page is the job detail with an Apply button.
        # Never hop from a URL that is ALREADY a terminal ATS form (avoids wandering off
        # a real Greenhouse/Lever form onto a marketing/"apply" landing page).
        if (_depth <= 1 and not is_terminal_form_url(landing)
                and not await _has_real_form(page)):
            apply_link = await _find_apply_link(page, landing)
            if apply_link and apply_link.rstrip("/").lower() != landing.rstrip("/").lower():
                logger.info("apply_link_followed", title=title[:50], apply=apply_link)
                return apply_link
        logger.info("apply_url_unresolved", title=title[:50], url=listing_url,
                    anchors=len(anchors))
        return None
    finally:
        try:
            await page.context.close()
        except Exception:
            pass


async def sync_resolved_to_db(updates: list[dict]) -> int:
    """Push resolved apply_urls into the DB the UI/extension read from: JobListing.apply_url
    and Application.ats_url, matched by (company_name, title) since pipeline ids and DB ids
    differ across stores. Returns rows touched. Best-effort — never raises."""
    if not updates:
        return 0
    try:
        from sqlalchemy import select
        from db.session import get_async_session
        from db.models import JobListing, Application

        def _norm(s: str) -> str:
            return re.sub(r"\s+", " ", (s or "").strip().lower())

        touched = 0
        async with get_async_session() as s:
            for u in updates:
                co, title, new = _norm(u["company"]), _norm(u["title"]), u["new"]
                rows = (await s.execute(select(JobListing).where(JobListing.company_name.isnot(None)))).scalars().all()
                jl = next((r for r in rows if _norm(r.company_name) == co and _norm(r.title) == title), None)
                if not jl:
                    continue
                jl.apply_url = new
                app = (await s.execute(select(Application).where(Application.job_id == jl.id))).scalar_one_or_none()
                if app:
                    app.ats_url = new
                touched += 1
            await s.commit()
        logger.info("db_sync_resolved", touched=touched)
        return touched
    except Exception as e:
        logger.warning("db_sync_resolved_failed", error=str(e)[:120])
        return 0


async def backfill_listing_urls(state: dict, job_ids: set | None = None,
                                limit: int | None = None) -> dict:
    """Repair jobs whose apply_url is a listing page → real per-job form URL.

    job_ids: only fix these job ids (e.g. the ones referenced by pending apps). If None,
             scan every job with a listing-style apply_url.
    Returns {checked, resolved, updates:[{job_id, company, title, old, new}]}.
    """
    from tools.browser import BrowserClient

    jobs = state.get("job_listings", [])
    targets = [j for j in jobs
               if (job_ids is None or j.get("id") in job_ids)
               and is_listing_url(j.get("apply_url") or "")]
    if limit:
        targets = targets[:limit]

    report = {"checked": 0, "resolved": 0, "updates": []}
    if not targets:
        return report

    logger.info("backfill_start", count=len(targets))
    async with BrowserClient(headless=True) as browser:
        for job in targets:
            report["checked"] += 1
            old = job.get("apply_url") or ""
            new = await resolve_one(browser, old or "", job.get("title") or "")
            if new and new != old:
                job["apply_url"] = new
                report["resolved"] += 1
                report["updates"].append({
                    "job_id": job.get("id"), "company": job.get("company_name"),
                    "title": job.get("title"), "old": old, "new": new,
                })
    logger.info("backfill_done", checked=report["checked"], resolved=report["resolved"])
    return report


# ── CLI: repair listing-page apply_urls in the live pipeline ────────────────────
# Usage: python -m agents.discovery.apply_url_resolver [--all] [--limit N]
#   default: only jobs referenced by apps still in pending_review/pending_manual_submit.
if __name__ == "__main__":
    import asyncio
    import json
    import sys
    from pathlib import Path

    _AL = Path("data/applied_jobs.json")

    async def _cli():
        from core.state_store import load_state as _ls
        state = _ls()
        jobs = {j["id"]: j for j in state["job_listings"]}
        limit = None
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        if "--all" in sys.argv:
            ids = None  # every listing-style URL
        else:
            live = {"pending_review", "pending_manual_submit", "blocked"}
            ids = {a["job_id"] for a in state["applications"]
                   if a.get("status") in live and a.get("job_id") in jobs
                   and is_listing_url(jobs[a["job_id"]].get("apply_url") or "")}
        rep = await backfill_listing_urls(state, job_ids=ids, limit=limit)
        from core.state_store import save_state as _ss
        _ss(state)   # locked + atomic + merge (was a raw write that could clobber the scheduler)
        # keep the applied-log's apply_url fallback in sync too
        if _AL.exists() and rep["updates"]:
            try:
                log = json.loads(_AL.read_text())
                new_by_jid = {u["job_id"]: u["new"] for u in rep["updates"]}
                for e in log:
                    if e.get("job_id") in new_by_jid:
                        e["apply_url"] = new_by_jid[e["job_id"]]
                _AL.write_text(json.dumps(log, indent=2))
            except Exception:
                pass
        touched = await sync_resolved_to_db(rep["updates"])
        print(f"resolved {rep['resolved']}/{rep['checked']} listing URLs; db rows touched: {touched}")
        for u in rep["updates"]:
            print(f"  {u['company'][:18]:18} -> {u['new']}")

    asyncio.run(_cli())
