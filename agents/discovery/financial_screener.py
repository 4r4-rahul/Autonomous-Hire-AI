"""
Financial Screener Agent
Screens publicly traded tech companies using yfinance financial data.
Logic: high revenue growth + strong profit margins + rising market cap
= company in expansion mode = likely hiring.

Complements company_finder (which covers private startups via web search).
"""

import asyncio
import uuid
from datetime import datetime
from typing import Any

import yfinance as yf

from core.state import Company, JobSearchState
from core.memory import Memory
from core.logger import get_logger

logger = get_logger("financial_screener")

# ── Tech company universe by domain ────────────────────────────────────────
# Grouped so we can filter by the user's target domains

TICKER_UNIVERSE: dict[str, list[str]] = {
    "AI/ML": [
        # Hyperscalers building AI infra
        "NVDA", "MSFT", "GOOGL", "META", "AMZN",
        # Pure-play AI / LLM companies (public)
        "PLTR", "AI", "PATH", "BBAI", "SOUN",
        # AI infrastructure & MLOps tooling
        "SNOW", "DDOG", "MDB", "ESTC", "DBX",
        # AI-native SaaS
        "NOW", "CRM", "ADBE",
    ],
    "LLM/GenAI": [
        # Companies with large GenAI engineering teams
        "MSFT",   # Azure OpenAI, Copilot
        "GOOGL",  # Gemini, Vertex AI
        "META",   # LLaMA, AI Research
        "AMZN",   # AWS Bedrock, Alexa AI
        "NVDA",   # NIM, AI Enterprise
        "PLTR",   # AIP platform
        "AI",     # C3.ai
        "SNOW",   # Cortex AI
        "DBX",    # DBRX, MosaicML
    ],
    "FinTech": [
        "V", "MA", "PYPL", "SQ", "AFRM",
        "COIN", "HOOD", "SOFI", "NU", "UPST",
        "FIS", "FISV", "GPN", "ADYEY",
    ],
    "Developer Tools": [
        "GTLB", "HUBS", "ESTC", "DDOG", "PD",
        "TEAM", "NOW", "OKTA", "ZS", "CRWD",
        "NET", "FSLY", "TWLO",
    ],
    "Data Infrastructure": [
        "SNOW", "MDB", "DDOG", "ESTC", "DBX",
        "CLDR", "NTAP", "PSTG", "VMW",
    ],
    "Healthcare Tech": [
        "VEEV", "HCAT", "PHYC", "DOCS", "ACCD",
        "ONEM", "TDOC", "CERT",
    ],
    "Cloud / SaaS": [
        "CRM", "NOW", "WDAY", "ADBE", "INTU",
        "ZM", "RNG", "FIVE", "PCTY", "PAYC",
        "SMAR", "ASAN", "MNDY",
    ],
}

# Size classification by market cap (USD)
def _classify_size(market_cap: float | None) -> str:
    if not market_cap:
        return "unknown"
    if market_cap < 2e9:
        return "startup"
    if market_cap < 10e9:
        return "scaleup"
    if market_cap < 100e9:
        return "mid"
    return "large"


def _safe_float(val: Any) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _score_company(info: dict) -> tuple[float, dict]:
    """
    Score a company on hiring likelihood (0–100).
    Returns (score, reasoning_dict).

    Signals:
      - Revenue growth YoY        (0–30 pts)  — growing top line = needs people
      - Profit margin             (0–25 pts)  — can afford to hire
      - Market cap growth 52w     (0–20 pts)  — investor confidence = expansion
      - R&D spend ratio           (0–15 pts)  — R&D heavy = engineering headcount
      - Employee count signal     (0–10 pts)  — bonus if employees > 100
    """
    score = 0.0
    reasons: dict[str, Any] = {}

    # 1. Revenue growth YoY (reported as decimal, e.g. 0.23 = 23%)
    rev_growth = _safe_float(info.get("revenueGrowth"))
    reasons["revenue_growth_pct"] = round(rev_growth * 100, 1)
    if rev_growth >= 0.30:
        score += 30
    elif rev_growth >= 0.15:
        score += 20
    elif rev_growth >= 0.05:
        score += 10
    elif rev_growth > 0:
        score += 5

    # 2. Profit margin (net income / revenue)
    profit_margin = _safe_float(info.get("profitMargins"))
    reasons["profit_margin_pct"] = round(profit_margin * 100, 1)
    if profit_margin >= 0.20:
        score += 25
    elif profit_margin >= 0.10:
        score += 18
    elif profit_margin >= 0.0:
        score += 10
    # Negative margin = unprofitable, 0 pts

    # 3. 52-week price change as market cap growth proxy
    price_52w_low = _safe_float(info.get("fiftyTwoWeekLow"))
    price_52w_high = _safe_float(info.get("fiftyTwoWeekHigh"))
    current_price = _safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))
    if price_52w_low > 0 and current_price > 0:
        pct_from_low = (current_price - price_52w_low) / price_52w_low
        reasons["price_pct_from_52w_low"] = round(pct_from_low * 100, 1)
        if pct_from_low >= 0.50:
            score += 20
        elif pct_from_low >= 0.25:
            score += 14
        elif pct_from_low >= 0.10:
            score += 8

    # 4. R&D spend ratio (R&D / revenue)
    total_revenue = _safe_float(info.get("totalRevenue"))
    rd_spend = _safe_float(info.get("researchAndDevelopment") or info.get("researchDevelopment"))
    if total_revenue > 0 and rd_spend > 0:
        rd_ratio = rd_spend / total_revenue
        reasons["rd_ratio_pct"] = round(rd_ratio * 100, 1)
        if rd_ratio >= 0.20:
            score += 15
        elif rd_ratio >= 0.10:
            score += 10
        elif rd_ratio >= 0.05:
            score += 5

    # 5. Employee count bonus (signals the company actually has a workforce)
    employees = info.get("fullTimeEmployees") or 0
    reasons["employees"] = employees
    if employees >= 1000:
        score += 10
    elif employees >= 100:
        score += 5

    return min(score, 100.0), reasons


async def _fetch_ticker(ticker: str) -> dict | None:
    """Fetch yfinance info for one ticker in a thread (yfinance is sync)."""
    try:
        info = await asyncio.to_thread(lambda: yf.Ticker(ticker).info)
        if not info or info.get("quoteType") == "NONE":
            return None
        return info
    except Exception as e:
        logger.warning("yfinance_fetch_failed", ticker=ticker, error=str(e))
        return None


async def run(
    state: JobSearchState,
    min_score: float = 50.0,
    max_companies: int = 30,
) -> JobSearchState:
    """
    Screen public tech companies and add high-scoring ones to state.
    Only adds companies that aren't already present (deduplication by ticker/name).
    """
    import yaml
    from pathlib import Path

    profile = yaml.safe_load(Path("data/user_profile.yaml").read_text())
    target_domains = set(profile.get("target", {}).get("domains", []))
    target_sizes = set(profile.get("target", {}).get("company_sizes", []))
    blacklist = set(profile.get("preferences", {}).get("blacklist_companies", []) or [])
    existing_names = {c["name"].lower() for c in state.get("companies", [])}

    memory = Memory.get()

    # Build ticker list filtered to user's target domains
    tickers_to_check: list[tuple[str, str]] = []  # (ticker, domain)
    for domain, tickers in TICKER_UNIVERSE.items():
        # Check if this domain overlaps with user targets (fuzzy)
        domain_match = any(
            d.lower() in domain.lower() or domain.lower() in d.lower()
            for d in target_domains
        )
        if domain_match or not target_domains:
            for t in tickers:
                tickers_to_check.append((t, domain))

    # Deduplicate tickers (same ticker may appear in multiple domains)
    seen_tickers: set[str] = set()
    unique_tickers = []
    for ticker, domain in tickers_to_check:
        if ticker not in seen_tickers:
            seen_tickers.add(ticker)
            unique_tickers.append((ticker, domain))

    logger.info("financial_screener_start", ticker_count=len(unique_tickers))

    # Fetch all tickers concurrently (batch of 10 to avoid hammering yfinance)
    scored: list[tuple[float, dict, str, str]] = []  # (score, info, ticker, domain)

    for i in range(0, len(unique_tickers), 10):
        batch = unique_tickers[i:i + 10]
        tasks = [_fetch_ticker(ticker) for ticker, _ in batch]
        results = await asyncio.gather(*tasks)

        for (ticker, domain), info in zip(batch, results):
            if not info:
                continue
            score, reasons = _score_company(info)
            scored.append((score, info, ticker, domain))

        # Small pause between batches to be polite
        await asyncio.sleep(1.0)

    # Sort by score descending, take top candidates above threshold
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [(s, info, t, d) for s, info, t, d in scored if s >= min_score]

    new_companies: list[Company] = []

    for score, info, ticker, domain in top[:max_companies]:
        name = info.get("longName") or info.get("shortName") or ticker
        if not name:
            continue
        if name.lower() in existing_names:
            continue
        if any(b.lower() in name.lower() for b in blacklist if b):
            continue

        # Size filter: only include if matches user's target sizes
        size = _classify_size(info.get("marketCap"))
        if target_sizes and size not in target_sizes and size != "unknown":
            pass  # still include — public companies are often "large" but still hire

        tech_stack = _infer_tech_stack(info)

        company: Company = {
            "id": str(uuid.uuid4()),
            "name": name,
            "website": info.get("website") or f"https://finance.yahoo.com/quote/{ticker}",
            "linkedin_url": "",
            "size": size,
            "domain": domain,
            "tech_stack": tech_stack,
            "open_role_count": 0,
            "discovered_at": datetime.utcnow().isoformat(),
        }

        # Attach financial metadata for job_scanner and fit_analyst to use
        company["_financial"] = {  # type: ignore[typeddict-unknown-key]
            "ticker": ticker,
            "market_cap": info.get("marketCap"),
            "revenue_growth_pct": score,
            "hiring_score": round(score, 1),
            "employees": info.get("fullTimeEmployees"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
        }

        memory.add_company(company["id"], {k: v for k, v in company.items() if not k.startswith("_")})
        new_companies.append(company)
        existing_names.add(name.lower())

        logger.log_decision(
            action="company_screened",
            outcome="added",
            details={
                "name": name,
                "ticker": ticker,
                "hiring_score": round(score, 1),
                "size": size,
                "domain": domain,
            },
        )

    logger.info(
        "financial_screener_complete",
        screened=len(scored),
        above_threshold=len(top),
        added=len(new_companies),
    )

    updated_state = dict(state)
    updated_state["companies"] = state.get("companies", []) + new_companies
    return updated_state


def _infer_tech_stack(info: dict) -> list[str]:
    """Infer likely tech stack from sector/industry/description keywords."""
    stack: list[str] = []
    text = " ".join([
        info.get("sector") or "",
        info.get("industry") or "",
        info.get("longBusinessSummary") or "",
    ]).lower()

    keyword_map = {
        "cloud": "AWS/Azure/GCP",
        "machine learning": "ML/AI",
        "artificial intelligence": "ML/AI",
        "data": "Data Engineering",
        "kubernetes": "Kubernetes",
        "python": "Python",
        "java": "Java",
        "fintech": "FinTech Stack",
        "payments": "Payments Infrastructure",
        "saas": "SaaS",
        "api": "REST APIs",
        "cybersecurity": "Security Engineering",
        "blockchain": "Blockchain",
    }
    for keyword, label in keyword_map.items():
        if keyword in text and label not in stack:
            stack.append(label)

    return stack[:6]


# ── CLI smoke test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    async def _test():
        empty_state: JobSearchState = {
            "companies": [], "job_listings": [], "fit_scores": [],
            "applications": [], "pending_approvals": [], "recruiter_contacts": [],
            "email_events": [], "meetings_scheduled": [],
            "weekly_quality_score": 0.0, "weekly_quantity_count": 0,
            "response_rate": 0.0, "interview_rate": 0.0,
            "last_run_at": None, "errors": [], "agent_statuses": {},
        }
        result = await run(empty_state, min_score=50.0, max_companies=20)
        print(f"\nTop companies by hiring score:")
        for c in result["companies"]:
            fin = c.get("_financial", {})
            print(f"  {c['name']:35s} ({c['size']:8s})  score={fin.get('hiring_score', '?'):5}  "
                  f"employees={fin.get('employees', '?')}")

    asyncio.run(_test())
