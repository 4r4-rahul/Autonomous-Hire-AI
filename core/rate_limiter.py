"""
Redis token-bucket rate limiter with 22-day warmup ramp.
Each channel has per-hour and per-day caps; warmup multiplier reduces them
until account age reaches 22 days.
"""
from __future__ import annotations

import math
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import redis
from redis import Redis

from core.config import get_settings

# ---------------------------------------------------------------------------
# Channel limits (raw caps at full ramp; warmup multiplier applied at runtime)
# ---------------------------------------------------------------------------
_LIMITS: dict[str, dict[str, int]] = {
    "linkedin_search":       {"per_hour": 10, "per_day": 60},   # 10 queries/hr covers full query list
    "linkedin_job_view":     {"per_hour": 40, "per_day": 200},  # ~7 results × 10 queries = 70 max
    "linkedin_profile_view": {"per_hour": 8,  "per_day": 50},
    "linkedin_connect_request": {"per_hour": 3, "per_day": 15},
    "linkedin_message":      {"per_hour": 4,  "per_day": 20},
    "external_application":  {"per_hour": 5,  "per_day": 25},
}

_FIRST_RUN_FILE = Path("data/linkedin_first_run.txt")


# ---------------------------------------------------------------------------
# Warmup ramp
# ---------------------------------------------------------------------------

def _account_age_days() -> int:
    """Days since first LinkedIn run (creates marker file on first call)."""
    if not _FIRST_RUN_FILE.exists():
        _FIRST_RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FIRST_RUN_FILE.write_text(date.today().isoformat())
        return 0
    try:
        raw = _FIRST_RUN_FILE.read_text().strip()
        # Accept plain date (2026-04-28) or full ISO timestamp (2026-04-28T...)
        start = date.fromisoformat(raw[:10])
        return (date.today() - start).days
    except (ValueError, IndexError):
        return 0


def _warmup_multiplier(age_days: int) -> float:
    if age_days < 8:
        return 0.30
    if age_days < 15:
        return 0.50
    if age_days < 22:
        return 0.75
    return 1.00


def _effective_cap(channel: str, window: str) -> int:
    """Return the actual cap for a channel+window after warmup scaling."""
    raw = _LIMITS[channel][window]
    mult = _warmup_multiplier(_account_age_days())
    return max(1, math.floor(raw * mult))


# ---------------------------------------------------------------------------
# Lua scripts for atomic token-bucket operations
# ---------------------------------------------------------------------------

# KEYS[1] = bucket key, ARGV[1] = capacity, ARGV[2] = window_seconds, ARGV[3] = tokens_requested
# Returns 1 if consumed, 0 if denied
_LUA_CHECK_CONSUME = """
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local window   = tonumber(ARGV[2])
local tokens   = tonumber(ARGV[3])

local current = tonumber(redis.call('GET', key) or '0')
if current + tokens > capacity then
    return 0
end
local new_val = redis.call('INCRBY', key, tokens)
if new_val == tokens then
    -- Key was just created; set TTL
    redis.call('EXPIRE', key, window)
end
return 1
"""

# Returns remaining tokens for KEYS[1] given ARGV[1] = capacity
_LUA_REMAINING = """
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local current  = tonumber(redis.call('GET', key) or '0')
return capacity - current
"""


class RateLimiter:
    """Per-channel Redis token bucket. Thread-safe via Lua atomicity."""

    def __init__(self, redis_client: Optional[Redis] = None) -> None:
        if redis_client is not None:
            self._r = redis_client
        else:
            self._r = redis.from_url(get_settings().redis_url, decode_responses=True)
        self._consume_script = self._r.register_script(_LUA_CHECK_CONSUME)
        self._remaining_script = self._r.register_script(_LUA_REMAINING)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_and_consume(self, channel: str, tokens: int = 1) -> bool:
        """
        Try to consume `tokens` from channel. Returns True if allowed,
        False if either the hourly or daily bucket is exhausted.
        """
        if channel not in _LIMITS:
            raise ValueError(f"Unknown rate-limit channel: {channel!r}")

        hour_key = self._key(channel, "hour")
        day_key  = self._key(channel, "day")
        hour_cap = _effective_cap(channel, "per_hour")
        day_cap  = _effective_cap(channel, "per_day")

        # Check both buckets before consuming either (two-phase check)
        hour_ok = self._can_consume(hour_key, hour_cap, 3600, tokens)
        day_ok  = self._can_consume(day_key,  day_cap,  86400, tokens)

        if not (hour_ok and day_ok):
            return False

        # Atomically consume from both
        hr_result  = self._consume_script(keys=[hour_key], args=[hour_cap, 3600, tokens])
        day_result = self._consume_script(keys=[day_key],  args=[day_cap,  86400, tokens])

        # If day bucket rejected after hour succeeded, roll back hour
        if not day_result and hr_result:
            self._r.decrby(hour_key, tokens)

        return bool(hr_result and day_result)

    def remaining(self, channel: str) -> dict[str, int]:
        """Return {'hour': N, 'day': N} remaining tokens for a channel."""
        if channel not in _LIMITS:
            raise ValueError(f"Unknown rate-limit channel: {channel!r}")

        hour_cap = _effective_cap(channel, "per_hour")
        day_cap  = _effective_cap(channel, "per_day")

        hour_rem = int(self._remaining_script(keys=[self._key(channel, "hour")], args=[hour_cap]))
        day_rem  = int(self._remaining_script(keys=[self._key(channel, "day")],  args=[day_cap]))

        return {"hour": max(0, hour_rem), "day": max(0, day_rem)}

    def status_all(self) -> dict[str, dict[str, int]]:
        """Remaining tokens for every channel."""
        return {ch: self.remaining(ch) for ch in _LIMITS}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _can_consume(self, key: str, capacity: int, window: int, tokens: int) -> bool:
        current = int(self._r.get(key) or 0)
        return current + tokens <= capacity

    @staticmethod
    def _key(channel: str, window: str) -> str:
        """Redis key scoped to calendar hour or day so buckets auto-reset."""
        now = datetime.utcnow()
        if window == "hour":
            stamp = now.strftime("%Y%m%d%H")
        else:
            stamp = now.strftime("%Y%m%d")
        return f"ratelimit:{channel}:{window}:{stamp}"


# ---------------------------------------------------------------------------
# Module-level singleton (lazy)
# ---------------------------------------------------------------------------
_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
