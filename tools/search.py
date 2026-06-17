"""
search_jobs  — scrape all 5 platforms concurrently with Playwright.
filter_jobs  — rank / filter results against the candidate profile.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any

from playwright.async_api import BrowserContext, Page, async_playwright

from datetime import datetime, timezone

from config import get_user_agent, load_config
from tools.profile import PROFILE, compute_match_score, should_exclude
from tools.session import load_cookies


def _parse_days_ago(text: str) -> int:
    """
    Parse posting age from various formats:
      "3 days ago", "1 week ago", "2 weeks ago", "1 month ago",
      "Few hours ago", "Just now", "Today", "30+ days ago"
    Returns days as int, or -1 if unparseable.
    """
    if not text:
        return -1
    t = text.lower().strip()
    if any(w in t for w in ("just now", "today", "few hours", "hour ago", "hours ago", "moment")):
        return 0
    m = re.search(r"(\d+)\s*day", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*week", t)
    if m:
        return int(m.group(1)) * 7
    m = re.search(r"(\d+)\s*month", t)
    if m:
        return int(m.group(1)) * 30
    if "yesterday" in t:
        return 1
    return -1


def _date_to_days_ago(date_str: str) -> int:
    """Parse an ISO/epoch date string and return days ago. Returns -1 on failure."""
    if not date_str:
        return -1
    try:
        # Naukri uses "DD MMM YYYY" like "14 Apr 2026"
        dt = datetime.strptime(date_str.strip(), "%d %b %Y")
        delta = datetime.now() - dt
        return max(0, delta.days)
    except Exception:
        pass
    try:
        # Try ISO format
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        return max(0, delta.days)
    except Exception:
        pass
    return -1

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class JobResult:
    title: str
    company: str
    location: str
    salary: str
    apply_url: str
    match_score: float
    platform: str
    description: str = ""
    posted_days_ago: int = -1  # -1 means unknown

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Per-platform search URL builders
# ---------------------------------------------------------------------------

def _naukri_url(keywords: str, location: str, experience: int) -> str:
    kw_slug = keywords.lower().replace(" ", "-").replace(",", "-")
    loc_slug = location.lower().replace(" ", "-").replace(",", "")
    # Use experience range: e.g. 3 years → search 3-5 year range
    exp_min = experience
    exp_max = experience + 2
    return (
        f"https://www.naukri.com/{kw_slug}-jobs-in-{loc_slug}"
        f"?experience={exp_min}&nignbelow_salary=0&salary=0&salaryType=0"
        f"&expmax={exp_max}"
    )


PLATFORM_BUILDERS: dict[str, Any] = {
    "naukri": _naukri_url,
}

# ---------------------------------------------------------------------------
# Per-platform scrapers  (each returns list[JobResult])
# ---------------------------------------------------------------------------

async def _detect_captcha(page: Page) -> bool:
    """Heuristic: look for common CAPTCHA indicators in page text (not HTML src)."""
    try:
        text = (await page.inner_text("body")).lower()
    except Exception:
        text = (await page.content()).lower()
    indicators = [
        "captcha", "recaptcha", "hcaptcha", "cf-challenge",
        "challenge-running", "verify you are human",
    ]
    # Avoid false positives from minified JS or attribute names
    return any(f" {ind}" in f" {text}" or text.startswith(ind) for ind in indicators)



async def _scrape_naukri(page: Page, url: str) -> list[JobResult]:
    """
    Scrape Naukri by intercepting its internal /jobapi/v3/search JSON API.
    Fetches multiple pages. Only returns **easy-apply** jobs.
    """
    results: list[JobResult] = []
    all_api_jobs: list[dict] = []
    api_data: dict | None = None

    async def _intercept(route, request):
        nonlocal api_data
        response = await route.fetch()
        if "jobapi/v3/search" in request.url:
            try:
                api_data = json.loads(await response.text())
            except Exception:
                pass
        await route.fulfill(response=response)

    try:
        await page.context.route("**/jobapi/**", _intercept)

        # Fetch up to 5 pages (20 jobs each = 100 jobs max per keyword)
        for page_num in range(1, 6):
            api_data = None
            page_url = url if page_num == 1 else f"{url}&pageNo={page_num}"
            await page.goto(page_url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(4000)

            if not api_data or "jobDetails" not in api_data:
                break  # No more pages

            page_jobs = api_data["jobDetails"]
            if not page_jobs:
                break  # Empty page

            all_api_jobs.extend(page_jobs)
            logger.info("Naukri page %d: %d jobs (total so far: %d)", page_num, len(page_jobs), len(all_api_jobs))

            # Stop if we got fewer than 20 (last page)
            if len(page_jobs) < 20:
                break

        logger.info("Naukri API total: %d jobs across pages", len(all_api_jobs))

        for job in all_api_jobs:
            # --- Filter: only easy/direct apply ---
            if job.get("companyApplyJob", False):
                continue  # skip external "Apply on company site"

            # --- Filter: skip jobs older than 30 days ---
            created = job.get("createdDate", "")
            days_ago = -1
            if isinstance(created, (int, float)) and created > 0:
                # Epoch timestamp (seconds or milliseconds)
                ts = created if created < 1e11 else created / 1000
                delta = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
                days_ago = max(0, delta.days)
            elif isinstance(created, str) and created:
                days_ago = _parse_days_ago(created)
                if days_ago == -1:
                    days_ago = _date_to_days_ago(created)
            if days_ago > 30:
                continue

            title = job.get("title", "")
            company = job.get("companyName", "")
            jd_url = job.get("jdURL", "")
            if jd_url and not jd_url.startswith("http"):
                jd_url = "https://www.naukri.com" + jd_url

            # Location from placeholders
            location = ""
            salary = "Not listed"
            for ph in job.get("placeholders", []):
                if ph.get("type") == "location":
                    location = ph.get("label", "")
                elif ph.get("type") == "salary":
                    salary = ph.get("label", "Not listed")

            # Skills for better matching
            skills_str = job.get("tagsAndSkills", "")
            skill_list = [s.strip() for s in skills_str.split(",") if s.strip()] if skills_str else []

            description = job.get("jobDescription", "")

            if title:
                score = compute_match_score(title, description, location, skill_list)
                results.append(JobResult(
                    title=title,
                    company=company,
                    location=location,
                    salary=salary,
                    apply_url=jd_url or url,
                    match_score=score,
                    platform="naukri",
                    description=description[:200],
                    posted_days_ago=days_ago,
                ))
    except Exception as exc:
        logger.error("Naukri scrape error: %s", exc)
    return results


PLATFORM_SCRAPERS = {
    "naukri": _scrape_naukri,
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def search_jobs(
    keywords: list[str] | None = None,
    location: str = "India",
    experience_years: int = 3,
    remote: bool = False,
    platforms: list[str] | None = None,  # kept for backwards-compat; ignored
) -> list[dict[str, Any]]:
    """
    Search Naukri.com for easy-apply jobs and return merged results.
    """
    if not keywords:
        keywords = list(PROFILE.default_search_keywords)

    kw_string = ", ".join(keywords)
    if remote:
        kw_string += " remote"

    platform_results: list = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=False)
        context = await browser.new_context(
            user_agent=get_user_agent(),
            viewport={"width": 1280, "height": 800},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            ignore_https_errors=True,
        )
        await load_cookies(context, "naukri")

        url = PLATFORM_BUILDERS["naukri"](kw_string, location, experience_years)
        page = await context.new_page()
        try:
            results = await _scrape_naukri(page, url)
            platform_results.append(results)
        except Exception as exc:
            logger.error("Naukri scrape failed: %s", exc)
            platform_results.append(exc)
        finally:
            await page.close()

        await browser.close()

    # Merge
    all_jobs: list[JobResult] = []
    for res in platform_results:
        if isinstance(res, list):
            all_jobs.extend(res)
        else:
            logger.error("Platform scrape failed: %s", res)

    # Sort by match score descending
    all_jobs.sort(key=lambda j: j.match_score, reverse=True)
    return [j.to_dict() for j in all_jobs]


def filter_jobs(
    jobs: list[dict[str, Any]],
    min_match_score: float = 0.7,
    max_days_old: int = 30,
) -> list[dict[str, Any]]:
    """
    Filter and rank jobs from search_jobs output.
    - Removes jobs below min_match_score
    - Excludes jobs in avoid categories
    - Excludes jobs posted more than max_days_old days ago
    - Returns top 20
    """
    filtered: list[dict[str, Any]] = []
    for job in jobs:
        if job.get("match_score", 0) < min_match_score:
            continue
        if should_exclude(job.get("title", ""), job.get("description", "")):
            continue
        # Skip jobs older than max_days_old (allow -1 = unknown through)
        days = job.get("posted_days_ago", -1)
        if days > max_days_old:
            continue
        filtered.append(job)

    filtered.sort(key=lambda j: j.get("match_score", 0), reverse=True)
    return filtered[:50]
