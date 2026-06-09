"""
Human-like timing and mouse-movement helpers for browser automation.
All delays are probabilistic so they never produce the same pattern twice.
"""
from __future__ import annotations

import asyncio
import math
import random
from typing import Tuple

from playwright.async_api import Page


# ---------------------------------------------------------------------------
# Typing delays
# ---------------------------------------------------------------------------

async def type_like_human(page: Page, selector: str, text: str) -> None:
    """
    Click into `selector` then type `text` with gamma-distributed inter-key delays.
    Mean ~120 ms, occasional pauses for "thinking".
    """
    await page.click(selector)
    await asyncio.sleep(_jitter(0.3, 0.15))

    for i, char in enumerate(text):
        # Gamma(shape=2, scale=60ms) → mean 120 ms, right-skewed
        delay_ms = random.gammavariate(2, 60)
        # Occasional longer pause every ~15 chars (like hesitating)
        if i > 0 and i % random.randint(12, 20) == 0:
            delay_ms += random.uniform(400, 1200)
        await page.keyboard.type(char, delay=delay_ms)


# ---------------------------------------------------------------------------
# Mouse movement
# ---------------------------------------------------------------------------

def _bezier_curve(
    p0: Tuple[float, float],
    p1: Tuple[float, float],
    p2: Tuple[float, float],
    p3: Tuple[float, float],
    steps: int = 20,
) -> list[Tuple[float, float]]:
    """Cubic Bézier from p0→p3 with control points p1, p2."""
    pts = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        x = mt**3*p0[0] + 3*mt**2*t*p1[0] + 3*mt*t**2*p2[0] + t**3*p3[0]
        y = mt**3*p0[1] + 3*mt**2*t*p1[1] + 3*mt*t**2*p2[1] + t**3*p3[1]
        pts.append((x, y))
    return pts


async def move_mouse_human(page: Page, x: float, y: float) -> None:
    """
    Move mouse to (x, y) along a random Bézier curve rather than a straight line.
    """
    current = await page.evaluate("() => ({x: window._mouseX || 0, y: window._mouseY || 0})")
    x0, y0 = float(current.get("x", 0)), float(current.get("y", 0))

    # Random control points that curve the path naturally
    spread = math.hypot(x - x0, y - y0) * 0.4
    cp1 = (x0 + (x - x0) * 0.3 + random.uniform(-spread, spread),
           y0 + (y - y0) * 0.3 + random.uniform(-spread, spread))
    cp2 = (x0 + (x - x0) * 0.7 + random.uniform(-spread, spread),
           y0 + (y - y0) * 0.7 + random.uniform(-spread, spread))

    points = _bezier_curve((x0, y0), cp1, cp2, (x, y), steps=random.randint(15, 30))
    for px, py in points:
        await page.mouse.move(px, py)
        await asyncio.sleep(random.uniform(0.005, 0.020))


async def click_human(page: Page, selector: str) -> None:
    """Move to element center along Bézier then click with small position jitter."""
    elem = page.locator(selector).first
    box = await elem.bounding_box()
    if box:
        cx = box["x"] + box["width"] / 2 + random.uniform(-4, 4)
        cy = box["y"] + box["height"] / 2 + random.uniform(-4, 4)
        await move_mouse_human(page, cx, cy)
        await asyncio.sleep(_jitter(0.1, 0.06))
    await elem.click()


# ---------------------------------------------------------------------------
# Reading / page-interaction pauses
# ---------------------------------------------------------------------------

async def reading_pause(char_count: int) -> None:
    """
    Sleep proportional to text length, simulating reading speed (~250 wpm).
    Adds uniform jitter ±30%.
    """
    words = max(1, char_count // 5)
    base_secs = (words / 250) * 60          # 250 wpm
    base_secs = max(0.5, min(base_secs, 8)) # clamp 0.5s–8s
    await asyncio.sleep(base_secs * random.uniform(0.7, 1.3))


async def scroll_down_human(page: Page, pixels: int = 300) -> None:
    """Scroll in small increments with variable speed."""
    remaining = pixels
    while remaining > 0:
        chunk = random.randint(60, 140)
        await page.mouse.wheel(0, min(chunk, remaining))
        remaining -= chunk
        await asyncio.sleep(_jitter(0.12, 0.06))


async def page_think_pause() -> None:
    """Short "thinking" pause between actions (0.4–1.8 s)."""
    await asyncio.sleep(_jitter(1.1, 0.5))


async def session_idle(min_secs: float = 30, max_secs: float = 120) -> None:
    """Longer idle between major actions (tab switch, navigating away)."""
    await asyncio.sleep(random.uniform(min_secs, max_secs))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jitter(mean: float, std: float) -> float:
    """Gaussian-ish positive value; clipped to [mean*0.2, mean*3]."""
    v = random.gauss(mean, std)
    return max(mean * 0.2, min(v, mean * 3))
