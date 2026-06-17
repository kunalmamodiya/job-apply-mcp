"""
Browser session / cookie management for each job platform.

Cookies are stored as JSON files under ~/.job-apply-mcp/sessions/<platform>.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, async_playwright

from config import SESSIONS_DIR, ensure_dirs, get_user_agent

logger = logging.getLogger(__name__)

PLATFORM_LOGIN_URLS: dict[str, str] = {
    "naukri": "https://www.naukri.com/mnjuser/login",
}

SUPPORTED_PLATFORMS = tuple(PLATFORM_LOGIN_URLS.keys())


def _cookie_path(platform: str) -> Path:
    return SESSIONS_DIR / f"{platform}.json"


def has_session(platform: str) -> bool:
    """Return True if saved cookies exist for the platform."""
    return _cookie_path(platform).is_file()


async def load_cookies(context: BrowserContext, platform: str) -> bool:
    """
    Load saved cookies into a Playwright BrowserContext.
    Returns True if cookies were loaded.
    """
    path = _cookie_path(platform)
    if not path.is_file():
        logger.warning("No saved session for %s", platform)
        return False
    cookies = json.loads(path.read_text())
    await context.add_cookies(cookies)
    logger.info("Loaded %d cookies for %s", len(cookies), platform)
    return True


async def save_cookies_from_context(
    context: BrowserContext, platform: str
) -> int:
    """Persist current cookies from a BrowserContext to disk."""
    ensure_dirs()
    cookies = await context.cookies()
    path = _cookie_path(platform)
    path.write_text(json.dumps(cookies, indent=2))
    logger.info("Saved %d cookies for %s", len(cookies), platform)
    return len(cookies)


async def interactive_login(platform: str) -> dict[str, Any]:
    """
    Open a **visible** browser window so the user can log in manually.
    After the user closes the browser (or presses Enter in the terminal),
    save the session cookies.

    Returns a status dict.
    """
    platform = platform.lower().strip()
    if platform not in PLATFORM_LOGIN_URLS:
        return {
            "success": False,
            "error": f"Unsupported platform '{platform}'. Choose from: {', '.join(SUPPORTED_PLATFORMS)}",
        }

    url = PLATFORM_LOGIN_URLS[platform]
    ensure_dirs()

    async with async_playwright() as pw:
        # Use Firefox — Chromium gets TLS-fingerprint blocked by many job sites
        browser = await pw.chromium.launch(channel="chrome", headless=False)
        context = await browser.new_context(
            user_agent=get_user_agent(),
            viewport={"width": 1280, "height": 800},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            ignore_https_errors=True,
        )

        # Load existing cookies if any (lets user resume partial sessions)
        await load_cookies(context, platform)

        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # Wait for the user to finish logging in.
        # We watch for navigation away from the login page or for the page
        # to reach a logged-in state.  We give the user up to 5 minutes.
        try:
            logger.info(
                "Browser opened for %s login. Please log in manually. "
                "The session will be saved automatically when you close the browser "
                "or after 5 minutes of inactivity.",
                platform,
            )
            # Wait until the URL changes from the login page (indicating
            # successful login) or until the browser disconnects.
            await page.wait_for_url(
                lambda u: u != url,  # type: ignore[arg-type]
                timeout=300_000,  # 5 minutes
            )
            # Give the page a moment to settle after redirect
            await page.wait_for_timeout(3000)
        except Exception:
            # Timeout or user closed browser — save whatever we have
            pass

        count = await save_cookies_from_context(context, platform)
        await browser.close()

    return {
        "success": True,
        "platform": platform,
        "cookies_saved": count,
        "session_path": str(_cookie_path(platform)),
    }
