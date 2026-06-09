"""
Stealth layer — rate limiting and human-behavior simulation.
All agents import their rate limiter from here.
"""

import asyncio
import random
import time
from collections import defaultdict, deque
from datetime import datetime

RATE_LIMITS: dict[str, dict] = {
    "linkedin_profile_views": {"max": 20, "window_seconds": 3600},
    "linkedin_messages":      {"max": 10, "window_seconds": 86400},
    "job_board_requests":     {"max": 30, "window_seconds": 60},
    "company_page_visits":    {"max": 10, "window_seconds": 60},
    "search_queries":         {"max": 60, "window_seconds": 60},
}

DELAY_RANGE = (3.0, 12.0)       # seconds between actions
SESSION_LENGTH = (20, 45)        # minutes — vary session length
OFF_HOURS = (0, 6)               # pause midnight–6am local time

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
]

# Sliding window call tracker: action_type → deque of timestamps
_call_log: dict[str, deque] = defaultdict(deque)
_lock = asyncio.Lock()


async def throttle(action_type: str) -> None:
    """
    Block until the action is within rate limits, then record the call.
    Also adds a randomized human-like delay.
    """
    if action_type not in RATE_LIMITS:
        await _human_delay()
        return

    limit = RATE_LIMITS[action_type]
    max_calls = limit["max"]
    window = limit["window_seconds"]

    async with _lock:
        now = time.monotonic()
        calls = _call_log[action_type]

        # Evict entries outside the window
        while calls and now - calls[0] > window:
            calls.popleft()

        if len(calls) >= max_calls:
            oldest = calls[0]
            wait = window - (now - oldest) + random.uniform(1, 3)
            # Release lock while sleeping
            pass

        # Record this call
        _call_log[action_type].append(now)

    # Wait if needed (outside lock)
    if len(_call_log[action_type]) > max_calls:
        await asyncio.sleep(window / max_calls)

    await _human_delay()


async def _human_delay() -> None:
    """Random delay to simulate human pacing."""
    delay = random.uniform(*DELAY_RANGE)
    await asyncio.sleep(delay)


def is_off_hours() -> bool:
    hour = datetime.now().hour
    return OFF_HOURS[0] <= hour < OFF_HOURS[1]


def random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def jitter(base_seconds: float, pct: float = 0.3) -> float:
    """Add ±pct% jitter to a base delay."""
    delta = base_seconds * pct
    return base_seconds + random.uniform(-delta, delta)
