"""
Hard daily spend cap for all LLM usage.

Every Anthropic call goes through core.llm.LLMClient.chat, which records its cost here and refuses
to start a new call once the day's spend reaches the cap. This GUARANTEES the system never spends
more than the configured ceiling per day (default $4) regardless of how much work the scheduler
queues — the user's explicit constraint ("no more than $3-5/day").

Cap is configurable via env AUTONOMOUSHIRE_DAILY_USD_CAP (dollars). Spend is persisted per local
date to data/daily_spend.json so the cap holds across process restarts (scheduler runs many times
a day). Resets automatically at local midnight.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from filelock import FileLock

_FILE = Path("data/daily_spend.json")
# Cross-process lock: the scheduler AND the API both record spend; a threading.Lock is in-process
# only, so concurrent records lost-updated the total and silently blew the cap. filelock fixes that.
_LOCK = FileLock(str(_FILE.with_suffix(".lock")), timeout=10)
_DEFAULT_CAP = 4.0


class BudgetExceededError(RuntimeError):
    """Raised when a new LLM call would exceed the daily spend cap."""


def daily_cap() -> float:
    try:
        return float(os.getenv("AUTONOMOUSHIRE_DAILY_USD_CAP", _DEFAULT_CAP))
    except Exception:
        return _DEFAULT_CAP


def _today() -> str:
    return str(date.today())


def _load() -> dict:
    try:
        return json.loads(_FILE.read_text())
    except Exception:
        return {}


def spent_today() -> float:
    return float(_load().get(_today(), 0.0))


def remaining() -> float:
    return max(0.0, daily_cap() - spent_today())


def over_budget() -> bool:
    return spent_today() >= daily_cap()


def record(cost: float) -> None:
    """Add a call's cost to today's running total (persisted, pruned to ~14 days)."""
    if not cost:
        return
    with _LOCK:
        d = _load()            # re-read UNDER the lock so concurrent records don't lost-update
        k = _today()
        d[k] = round(float(d.get(k, 0.0)) + float(cost), 6)
        cutoff = str(date.today() - timedelta(days=14))
        d = {day: v for day, v in d.items() if day >= cutoff}
        try:
            _FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, _FILE)   # atomic
        except Exception:
            pass
