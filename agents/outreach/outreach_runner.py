"""
LinkedIn Outreach Runner.
Drives the cadence state machine: drafts messages, sends them via browser,
updates contact state. Respects per-channel rate limits and kill switch.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional

from playwright.async_api import Page

from agents.outreach.cadence import (
    advance_stage,
    contacts_ready_for_action,
    draft_message,
    should_archive,
)
from core.config import get_settings
from core.logger import get_logger
from core.rate_limiter import get_rate_limiter
from core.state import LinkedInContact, JobSearchState
from tools.humanize import (
    click_human,
    page_think_pause,
    reading_pause,
    type_like_human,
)
from tools.linkedin_browser import LinkedInBrowser

logger = get_logger("outreach_runner")


async def _is_paused() -> bool:
    settings = get_settings()
    if settings.linkedin_pause:
        return True
    hour = datetime.now().hour
    return not (settings.linkedin_active_hours_start <= hour < settings.linkedin_active_hours_end)


# ── Stage dispatchers ──────────────────────────────────────────────────────

async def _send_connection_request(
    browser: LinkedInBrowser,
    contact: LinkedInContact,
    note: str,
) -> bool:
    """Navigate to profile, click Connect, add note, send. Returns success."""
    limiter = get_rate_limiter()
    if not limiter.check_and_consume("linkedin_connect_request"):
        logger.info("rate_limited_connect", contact=contact["full_name"])
        return False

    page = await browser.new_page()
    try:
        await browser.goto(page, contact["li_profile_url"])
        await page_think_pause()

        # Click "Connect" button (may need to expand "More" menu first)
        connect_btn = page.locator("button:has-text('Connect')").first
        if not await connect_btn.is_visible(timeout=4000):
            more_btn = page.locator("button:has-text('More')").first
            if await more_btn.is_visible(timeout=3000):
                await click_human(page, "button:has-text('More')")
                await asyncio.sleep(0.5)
                connect_btn = page.locator("div[aria-label='Connect with'] button, li:has-text('Connect') button").first

        await connect_btn.click()
        await asyncio.sleep(0.8)

        # "Add a note" option
        add_note_btn = page.locator("button:has-text('Add a note')").first
        if await add_note_btn.is_visible(timeout=3000):
            await click_human(page, "button:has-text('Add a note')")
            await asyncio.sleep(0.5)

        # Type the note
        textarea = page.locator("textarea#custom-message, textarea[name='message']").first
        if await textarea.is_visible(timeout=3000):
            await type_like_human(page, "textarea#custom-message, textarea[name='message']", note[:300])
        await reading_pause(len(note))

        # Send
        await click_human(page, "button:has-text('Send')")
        await asyncio.sleep(1.0)
        logger.info("connection_sent", contact=contact["full_name"])
        return True

    except Exception as e:
        logger.warning("connection_send_failed", contact=contact["full_name"], error=str(e))
        return False
    finally:
        await page.close()


async def _check_connection_accepted(
    browser: LinkedInBrowser,
    contact: LinkedInContact,
) -> bool:
    """Check if the connection request was accepted."""
    page = await browser.new_page()
    try:
        await browser.goto(page, contact["li_profile_url"])
        await page_think_pause()
        # "Message" button visible = connected
        msg_btn = page.locator("button:has-text('Message')").first
        return await msg_btn.is_visible(timeout=5000)
    except Exception:
        return False
    finally:
        await page.close()


async def _send_linkedin_message(
    browser: LinkedInBrowser,
    contact: LinkedInContact,
    message_text: str,
) -> bool:
    """Open messaging thread for a connected contact and send a message."""
    limiter = get_rate_limiter()
    if not limiter.check_and_consume("linkedin_message"):
        logger.info("rate_limited_message", contact=contact["full_name"])
        return False

    page = await browser.new_page()
    try:
        await browser.goto(page, contact["li_profile_url"])
        await page_think_pause()

        # Click Message button on profile
        await click_human(page, "button:has-text('Message')")
        await asyncio.sleep(1.0)

        # Wait for message input
        await page.wait_for_selector(
            ".msg-form__contenteditable, div[data-placeholder='Write a message…']",
            timeout=10_000,
        )
        msg_input = ".msg-form__contenteditable, div[data-placeholder='Write a message…']"
        await type_like_human(page, msg_input, message_text)
        await reading_pause(len(message_text))

        # Send
        send_btn = page.locator("button.msg-form__send-button, button[type='submit']:has-text('Send')").first
        await send_btn.click()
        await asyncio.sleep(1.0)
        logger.info("message_sent", contact=contact["full_name"], stage=contact["cadence_stage"])
        return True

    except Exception as e:
        logger.warning("message_send_failed", contact=contact["full_name"], error=str(e))
        return False
    finally:
        await page.close()


# ── Main processing loop ───────────────────────────────────────────────────

async def _process_contact(
    browser: LinkedInBrowser,
    contact: LinkedInContact,
    job_title: str = "",
) -> LinkedInContact:
    """Process one contact through its next cadence action. Returns updated contact."""
    stage = contact["cadence_stage"]

    # Stage 6 = archived, skip
    if stage >= 6:
        return contact

    # Archive if timed out at followup stage
    if should_archive(contact):
        logger.info("archiving_contact", contact=contact["full_name"])
        return advance_stage(contact, 6)

    # ── Stage 0: send connection request ──────────────────────────────────
    if stage == 0:
        note = await draft_message(contact, "connection_request")
        if not note:
            return contact
        success = await _send_connection_request(browser, contact, note)
        if success:
            return advance_stage(contact, 1, message_body=note, stage_label="connection_note", status="sent")
        return contact

    # ── Stage 1: check if connection accepted ─────────────────────────────
    if stage == 1:
        accepted = await _check_connection_accepted(browser, contact)
        if accepted:
            return advance_stage(contact, 2)
        # If pending > 21 days, withdraw and archive
        nat = contact.get("last_action_at")
        if nat:
            from datetime import timedelta
            sent_at = datetime.fromisoformat(nat)
            if (datetime.now(timezone.utc) - sent_at).days > 21:
                logger.info("connection_expired", contact=contact["full_name"])
                return advance_stage(contact, 6)
        return contact

    # ── Stage 2: send thanks message ──────────────────────────────────────
    if stage == 2:
        msg = await draft_message(contact, "thanks_message")
        if not msg:
            return contact
        success = await _send_linkedin_message(browser, contact, msg)
        if success:
            return advance_stage(contact, 3, message_body=msg, stage_label="thanks", status="sent")
        return contact

    # ── Stage 3: send ask message (after 3-day wait) ──────────────────────
    if stage == 3:
        msg = await draft_message(contact, "ask_message", {"job_title": job_title})
        if not msg:
            return contact
        success = await _send_linkedin_message(browser, contact, msg)
        if success:
            return advance_stage(contact, 4, message_body=msg, stage_label="ask", status="sent")
        return contact

    # ── Stage 4: send followup (after 7-day wait) ─────────────────────────
    if stage == 4:
        original_ask = f"a quick 15-min call to learn about the team at {contact['company']}"
        msg = await draft_message(contact, "followup_message", {"original_ask": original_ask})
        if not msg:
            return contact
        success = await _send_linkedin_message(browser, contact, msg)
        if success:
            return advance_stage(contact, 5, message_body=msg, stage_label="followup_final", status="sent")
        return contact

    # ── Stage 5: archive after final wait ─────────────────────────────────
    if stage == 5:
        return advance_stage(contact, 6)

    return contact


async def run(state: JobSearchState) -> JobSearchState:
    """Advance all due contacts through the outreach cadence."""
    if await _is_paused():
        return state

    contacts = state.get("linkedin_contacts", [])
    due = contacts_ready_for_action(contacts)

    if not due:
        logger.info("outreach_runner_no_due_contacts")
        return state

    # Build job_id → title lookup
    job_map = {j["id"]: j["title"] for j in state.get("job_listings", [])}

    updated_contacts = {c["id"]: c for c in contacts}
    processed = 0

    try:
        async with LinkedInBrowser() as browser:
            page = await browser.new_page()
            if not await browser.is_logged_in(page):
                logger.warning("linkedin_not_logged_in")
                await page.close()
                return state
            await page.close()

            for contact in due:
                job_title = job_map.get(contact.get("related_job_id", ""), "")
                updated = await _process_contact(browser, contact, job_title=job_title)
                updated_contacts[updated["id"]] = updated
                processed += 1
                # Pause between contacts — 45-90 seconds
                await asyncio.sleep(45 + (hash(contact["id"]) % 45))

    except Exception as e:
        logger.error("outreach_runner_failed", error=str(e))

    logger.log_decision(
        action="outreach_run_complete",
        outcome="success",
        details={"contacts_processed": processed},
    )

    updated = dict(state)
    updated["linkedin_contacts"] = list(updated_contacts.values())
    return updated
