# State/local-government C2C lane — what works, and what the data says

Goal: raise contract/C2C supply by searching state-government work. This is what the public
surfaces actually turned out to be, measured on 2026-09-10.

## Where state-government contract work is (and isn't) reachable

| Surface | Reachable? | Why |
|---|---|---|
| State staff-aug program portals (VA VITA, MI DTMB, PA ITQ, NY HBITS) | **No** | Vendor-facing, not candidate-facing. VITA routes every requirement through CAI + VectorVMS; Michigan DTMB and NY HBITS are RFQ portals a *prequalified vendor* bids into. PA's Consulting Services ITQ explicitly excludes staff augmentation. A consultant cannot apply directly — the vendor submits you. |
| NEOGOV / governmentjobs.com (direct state-employee jobs) | **No, and low value anyway** | Knockout-rendered SPA behind reCAPTCHA with no public API — the fragile-browser branch this project avoids. The roles are also W2 civil service: they rarely sponsor and often carry residency rules. |
| Staff-aug **vendor** ATS boards (Greenhouse/Lever/Ashby/SmartRecruiters/Workable) | **Yes** | Plain HTTP JSON. This is the lane we built. |
| Dice / Ceipal / JobDiva / Bullhorn | Not yet | Where the *live* C2C reqs actually are. See "Next" below. |

So the only candidate-facing state-government contract surface is the vendor's own board.

## The lane

`agents/discovery/gov_scanner.py` scans `data/gov_vendors.json` and hands survivors to `ats_intake`.
Three filters run before anything is spent, because they protect a sponsorship-needing candidate:

* **Federal end clients dropped** — clearance / Public Trust / "US person" bars make them dead ends.
* **USC/GC-only dropped**, read from the **title** as well as the body. C2C boards encode the bar as
  a compact tag — `(USC/GC/TN/CTH)`, `Green Card/US Citizen Only` — and a SmartRecruiters *listing*
  carries no description at all, so title-blindness meant these were invisible. `core/gov_client.py`
  holds the patterns; the same check now backs `fit_analyst`'s sponsorship gate for every scanner.
* **Contract-lane titles.** Public-sector reqs are "Programmer Analyst" / "Java Developer" /
  "ETL Developer", never "Staff Software Engineer". Stacks outside the candidate's profile
  (.NET, SharePoint, PeopleSoft, SAP, Salesforce, mainframe) are excluded — a tailored résumé for
  those would be fabrication.

"W2 only" / "No C2C" is deliberately **not** treated as a citizenship bar: a vendor hiring on its own
W2 can still transfer an H1B, so blocking those would have thrown away viable roles.

Survivors are tagged with the government client (`gov_client` = level + state) on the JobListing.

## The measured yield — read this before expecting volume

Across the 28 verified vendor boards: **5,183 postings, of which 4,572 were over a year old and only
33 fell inside the 30-day freshness window.** After the in-lane title and US filters, **one** live
role remained. Two vendors (RESPEC, ProSidian) accounted for every fresh posting.

These SmartRecruiters boards are largely abandoned: vendors post once, never expire the req, and route
live work to Dice/Ceipal/JobDiva instead. The lane is correct; the *universe* is thin.

This is why the scanner drops stale postings and sorts newest-first **before** spending its JD-detail
budget — otherwise the whole budget goes to 2017 postings and the live req is never reached.

## Next, in value order

1. **Ceipal** — the C2C ATS these desks actually use. Public API pattern confirmed by reading
   `api.ceipal.com/careers_v3/js/app.min.js`: `GET https://api.ceipal.com/{api_key}/job-postings/`,
   with each portal exposing its own key at `{portal}/includes/config.inc.js`. Not yet validated —
   the one portal tested (`joblist.smartdata.net`) 404s, likely a dead tenant. Validate against a
   live portal before building an adapter.
2. **More vendor boards.** `scripts/expand_gov_vendors.py` promotes any candidate with a live board
   carrying US IT roles. Feed it slugs harvested by WebSearch over `jobs.smartrecruiters.com`;
   filter new additions by *fresh* posting count, not total.
3. **Dice** — the largest C2C board by far, and the one worth a bot-risk conversation.

## Commands

```
python scripts/scan_gov_now.py               # run the lane once (--gov-only, --employers, --dry-run)
python scripts/expand_gov_vendors.py         # re-probe the vendor universe (--dry-run, --slugs)
```

Scheduled as `gov_scanner` every 4h, live in both the default and contract job sets.
