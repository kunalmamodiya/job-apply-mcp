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

from config import load_config
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

def _linkedin_url(keywords: str, location: str, experience: int) -> str:
    params = {
        "keywords": keywords,
        "location": location,
        "f_E": "2,3,4",  # entry/associate/mid-senior
        "sortBy": "R",
    }
    return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)


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


def _wellfound_url(keywords: str, location: str, experience: int) -> str:
    params = {"q": keywords, "location": location}
    return "https://wellfound.com/jobs?" + urllib.parse.urlencode(params)


def _indeed_url(keywords: str, location: str, experience: int) -> str:
    params = {"q": keywords, "l": location, "fromage": "14"}
    return "https://in.indeed.com/jobs?" + urllib.parse.urlencode(params)


def _hirist_url(keywords: str, location: str, experience: int) -> str:
    params = {"q": keywords, "loc": location, "exp": str(experience)}
    return "https://www.hirist.tech/jobs?" + urllib.parse.urlencode(params)


def _glassdoor_url(keywords: str, location: str, experience: int) -> str:
    params = {"q": keywords, "l": location}
    return "https://www.glassdoor.co.in/Job/jobs.htm?" + urllib.parse.urlencode(params)


def _instahyre_url(keywords: str, location: str, experience: int) -> str:
    params = {"q": keywords, "location": location, "experience": str(experience)}
    return "https://www.instahyre.com/search-jobs/?" + urllib.parse.urlencode(params)


def _cutshort_url(keywords: str, location: str, experience: int) -> str:
    params = {"q": keywords, "location": location, "experience": f"{experience}-{experience + 2}"}
    return "https://cutshort.io/jobs?" + urllib.parse.urlencode(params)


PLATFORM_BUILDERS: dict[str, Any] = {
    "linkedin": _linkedin_url,
    "naukri": _naukri_url,
    "wellfound": _wellfound_url,
    "indeed": _indeed_url,
    "hirist": _hirist_url,
    "glassdoor": _glassdoor_url,
    "instahyre": _instahyre_url,
    "cutshort": _cutshort_url,
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


async def _scrape_linkedin(page: Page, url: str) -> list[JobResult]:
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on LinkedIn — skipping")
            return [JobResult(
                title="[CAPTCHA] LinkedIn requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="linkedin",
            )]
        await page.wait_for_timeout(2000)

        cards = await page.query_selector_all(
            "div.job-search-card, li.jobs-search-results__list-item, div.base-card"
        )
        for card in cards[:25]:
            title_el = await card.query_selector(
                "h3.base-search-card__title, a.job-card-list__title, h3"
            )
            company_el = await card.query_selector(
                "h4.base-search-card__subtitle, a.job-card-container__company-name, h4"
            )
            location_el = await card.query_selector(
                "span.job-search-card__location, li.job-card-container__metadata-item, span"
            )
            link_el = await card.query_selector("a[href*='/jobs/view'], a[href*='linkedin.com/jobs']")

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            href = await link_el.get_attribute("href") if link_el else ""

            if title:
                score = compute_match_score(title, "", location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary="Not listed", apply_url=href or url,
                    match_score=score, platform="linkedin",
                ))
    except Exception as exc:
        logger.error("LinkedIn scrape error: %s", exc)
    return results


async def _scrape_naukri(page: Page, url: str) -> list[JobResult]:
    """
    Scrape Naukri by intercepting its internal /jobapi/v3/search JSON API.
    Only returns **easy-apply** jobs (companyApplyJob == False).
    """
    results: list[JobResult] = []
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
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(5000)

        if not api_data or "jobDetails" not in api_data:
            logger.warning("Naukri: could not capture job API data")
            return results

        jobs = api_data["jobDetails"]
        logger.info("Naukri API: %d jobs returned", len(jobs))

        for job in jobs:
            # --- Filter: only easy/direct apply ---
            if job.get("companyApplyJob", False):
                continue  # skip external "Apply on company site"

            # --- Filter: skip jobs older than 30 days ---
            created = job.get("createdDate", "")
            days_ago = _parse_days_ago(created) if created else -1
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


async def _scrape_wellfound(page: Page, url: str) -> list[JobResult]:
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on Wellfound — skipping")
            return [JobResult(
                title="[CAPTCHA] Wellfound requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="wellfound",
            )]
        await page.wait_for_timeout(2000)

        cards = await page.query_selector_all(
            "div[class*='JobSearchResult'], div[class*='job-listing'], div[data-test='JobListing']"
        )
        for card in cards[:25]:
            title_el = await card.query_selector(
                "a[class*='jobTitle'], h2 a, a[data-test='job-title']"
            )
            company_el = await card.query_selector(
                "a[class*='company'], h2[class*='company'], a[data-test='startup-link']"
            )
            location_el = await card.query_selector(
                "span[class*='location'], span[data-test='location']"
            )
            salary_el = await card.query_selector(
                "span[class*='salary'], span[data-test='compensation']"
            )

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            salary = (await salary_el.inner_text()).strip() if salary_el else "Not listed"
            href = await title_el.get_attribute("href") if title_el else ""
            if href and not href.startswith("http"):
                href = "https://wellfound.com" + href

            if title:
                score = compute_match_score(title, "", location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary=salary, apply_url=href or url,
                    match_score=score, platform="wellfound",
                ))
    except Exception as exc:
        logger.error("Wellfound scrape error: %s", exc)
    return results


async def _scrape_indeed(page: Page, url: str) -> list[JobResult]:
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on Indeed — skipping")
            return [JobResult(
                title="[CAPTCHA] Indeed requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="indeed",
            )]
        await page.wait_for_timeout(2000)

        cards = await page.query_selector_all(
            "div.job_seen_beacon, div.jobsearch-SerpJobCard, td.resultContent"
        )
        for card in cards[:25]:
            title_el = await card.query_selector(
                "h2.jobTitle a, a[data-jk], span[title]"
            )
            company_el = await card.query_selector(
                "span[data-testid='company-name'], span.companyName, span.company"
            )
            location_el = await card.query_selector(
                "div[data-testid='text-location'], div.companyLocation, span.location"
            )
            salary_el = await card.query_selector(
                "div.salary-snippet-container, span.salary-snippet, div.metadata.salary-snippet-container"
            )

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            salary = (await salary_el.inner_text()).strip() if salary_el else "Not listed"

            href = ""
            if title_el:
                href = await title_el.get_attribute("href") or ""
            if href and not href.startswith("http"):
                href = "https://in.indeed.com" + href

            if title:
                score = compute_match_score(title, "", location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary=salary, apply_url=href or url,
                    match_score=score, platform="indeed",
                ))
    except Exception as exc:
        logger.error("Indeed scrape error: %s", exc)
    return results


async def _scrape_hirist(page: Page, url: str) -> list[JobResult]:
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on Hirist — skipping")
            return [JobResult(
                title="[CAPTCHA] Hirist requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="hirist",
            )]
        await page.wait_for_timeout(2000)

        cards = await page.query_selector_all(
            "div.job-card, div[class*='jobCard'], div.job-listing"
        )
        for card in cards[:25]:
            title_el = await card.query_selector("a[class*='title'], h3 a, a.job-title")
            company_el = await card.query_selector("span.company-name, a.company, div.company")
            location_el = await card.query_selector("span.location, div.location")
            salary_el = await card.query_selector("span.salary, div.salary")

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            salary = (await salary_el.inner_text()).strip() if salary_el else "Not listed"
            href = await title_el.get_attribute("href") if title_el else ""
            if href and not href.startswith("http"):
                href = "https://www.hirist.tech" + href

            if title:
                score = compute_match_score(title, "", location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary=salary, apply_url=href or url,
                    match_score=score, platform="hirist",
                ))
    except Exception as exc:
        logger.error("Hirist scrape error: %s", exc)
    return results


async def _scrape_glassdoor(page: Page, url: str) -> list[JobResult]:
    """Scrape Glassdoor job listings."""
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(4000)

        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on Glassdoor")
            return [JobResult(
                title="[CAPTCHA] Glassdoor requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="glassdoor",
            )]

        # Glassdoor job cards — try multiple selector patterns
        card_selectors = [
            "li[data-test='jobListing']",
            "li.JobsList_jobListItem__wjTHv",
            "li[class*='JobListItem']",
            "li[class*='react-job-listing']",
            "div[data-test='job-card']",
        ]
        cards = []
        for sel in card_selectors:
            cards = await page.query_selector_all(sel)
            if cards:
                logger.info("Glassdoor: matched %d cards with '%s'", len(cards), sel)
                break

        for card in cards[:25]:
            title_el = await card.query_selector(
                "a[data-test='job-link'], a[class*='JobCard_jobTitle'], "
                "a[class*='jobTitle'], a[href*='/job-listing/']"
            )
            company_el = await card.query_selector(
                "span[class*='EmployerProfile_compactEmployerName'], "
                "div[data-test='emp-name'], span[class*='companyName']"
            )
            location_el = await card.query_selector(
                "div[data-test='emp-location'], span[class*='location'], "
                "div[class*='JobCard_location']"
            )
            salary_el = await card.query_selector(
                "div[data-test='detailSalary'], span[class*='salary'], "
                "div[class*='JobCard_salary']"
            )

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            salary = (await salary_el.inner_text()).strip() if salary_el else "Not listed"
            href = await title_el.get_attribute("href") if title_el else ""
            if href and not href.startswith("http"):
                href = "https://www.glassdoor.co.in" + href

            if title:
                score = compute_match_score(title, "", location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary=salary, apply_url=href or url,
                    match_score=score, platform="glassdoor",
                ))

        # Fallback: extract from all job-listing links
        if not results:
            links = await page.query_selector_all("a[href*='/job-listing/']")
            for link in links[:25]:
                href = await link.get_attribute("href") or ""
                text = (await link.inner_text()).strip()
                if text and len(text) > 3:
                    if not href.startswith("http"):
                        href = "https://www.glassdoor.co.in" + href
                    score = compute_match_score(text, "", "India")
                    results.append(JobResult(
                        title=text, company="", location="India",
                        salary="Not listed", apply_url=href or url,
                        match_score=score, platform="glassdoor",
                    ))
    except Exception as exc:
        logger.error("Glassdoor scrape error: %s", exc)
    return results


async def _scrape_instahyre(page: Page, url: str) -> list[JobResult]:
    """Scrape Instahyre job listings."""
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(4000)

        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on Instahyre")
            return [JobResult(
                title="[CAPTCHA] Instahyre requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="instahyre",
            )]

        # Instahyre job cards
        card_selectors = [
            "div.opportunity-card",
            "div[class*='opportunity']",
            "div[class*='job-card']",
            "div[class*='JobCard']",
            "div.card[class*='job']",
        ]
        cards = []
        for sel in card_selectors:
            cards = await page.query_selector_all(sel)
            if cards:
                logger.info("Instahyre: matched %d cards with '%s'", len(cards), sel)
                break

        for card in cards[:25]:
            title_el = await card.query_selector(
                "a[class*='opportunity-title'], h3 a, a[class*='title'], "
                "div[class*='title'] a, a[href*='/opportunity/']"
            )
            company_el = await card.query_selector(
                "div[class*='company-name'], span[class*='company'], "
                "a[class*='company'], div[class*='companyName']"
            )
            location_el = await card.query_selector(
                "div[class*='location'], span[class*='location'], "
                "span[class*='city']"
            )
            salary_el = await card.query_selector(
                "div[class*='salary'], span[class*='salary'], "
                "div[class*='compensation']"
            )

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            salary = (await salary_el.inner_text()).strip() if salary_el else "Not listed"
            href = await title_el.get_attribute("href") if title_el else ""
            if href and not href.startswith("http"):
                href = "https://www.instahyre.com" + href

            if title:
                score = compute_match_score(title, "", location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary=salary, apply_url=href or url,
                    match_score=score, platform="instahyre",
                ))

        # Fallback: link extraction
        if not results:
            links = await page.query_selector_all("a[href*='/opportunity/']")
            for link in links[:25]:
                href = await link.get_attribute("href") or ""
                text = (await link.inner_text()).strip()
                if text and len(text) > 3:
                    if not href.startswith("http"):
                        href = "https://www.instahyre.com" + href
                    score = compute_match_score(text, "", "India")
                    results.append(JobResult(
                        title=text, company="", location="India",
                        salary="Not listed", apply_url=href or url,
                        match_score=score, platform="instahyre",
                    ))
    except Exception as exc:
        logger.error("Instahyre scrape error: %s", exc)
    return results


async def _scrape_cutshort(page: Page, url: str) -> list[JobResult]:
    """Scrape Cutshort job listings."""
    results: list[JobResult] = []
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(4000)

        if await _detect_captcha(page):
            logger.warning("CAPTCHA detected on Cutshort")
            return [JobResult(
                title="[CAPTCHA] Cutshort requires manual verification",
                company="", location="", salary="", apply_url=url,
                match_score=0, platform="cutshort",
            )]

        # Cutshort job cards
        card_selectors = [
            "div[class*='job-card']",
            "div[class*='JobCard']",
            "div[class*='jobCard']",
            "a[class*='job-card']",
            "div[class*='opportunity-card']",
            "div[class*='listing-card']",
        ]
        cards = []
        for sel in card_selectors:
            cards = await page.query_selector_all(sel)
            if cards:
                logger.info("Cutshort: matched %d cards with '%s'", len(cards), sel)
                break

        for card in cards[:25]:
            title_el = await card.query_selector(
                "a[class*='title'], h3 a, h2 a, "
                "div[class*='title'] a, a[href*='/job/']"
            )
            company_el = await card.query_selector(
                "div[class*='company'], span[class*='company'], "
                "a[class*='company'], p[class*='company']"
            )
            location_el = await card.query_selector(
                "div[class*='location'], span[class*='location'], "
                "span[class*='city']"
            )
            salary_el = await card.query_selector(
                "div[class*='salary'], span[class*='salary'], "
                "div[class*='compensation'], span[class*='ctc']"
            )
            skills_el = await card.query_selector(
                "div[class*='skills'], div[class*='tags'], "
                "div[class*='tech-stack']"
            )

            title = (await title_el.inner_text()).strip() if title_el else ""
            company = (await company_el.inner_text()).strip() if company_el else ""
            location = (await location_el.inner_text()).strip() if location_el else ""
            salary = (await salary_el.inner_text()).strip() if salary_el else "Not listed"
            skills_text = (await skills_el.inner_text()).strip() if skills_el else ""
            href = await title_el.get_attribute("href") if title_el else ""
            if href and not href.startswith("http"):
                href = "https://cutshort.io" + href

            if title:
                score = compute_match_score(title, skills_text, location)
                results.append(JobResult(
                    title=title, company=company, location=location,
                    salary=salary, apply_url=href or url,
                    match_score=score, platform="cutshort",
                ))

        # Fallback: link extraction
        if not results:
            links = await page.query_selector_all("a[href*='/job/']")
            for link in links[:25]:
                href = await link.get_attribute("href") or ""
                text = (await link.inner_text()).strip()
                if text and len(text) > 3 and "/job/" in href:
                    if not href.startswith("http"):
                        href = "https://cutshort.io" + href
                    score = compute_match_score(text, "", "India")
                    results.append(JobResult(
                        title=text, company="", location="India",
                        salary="Not listed", apply_url=href or url,
                        match_score=score, platform="cutshort",
                    ))
    except Exception as exc:
        logger.error("Cutshort scrape error: %s", exc)
    return results


PLATFORM_SCRAPERS = {
    "linkedin": _scrape_linkedin,
    "naukri": _scrape_naukri,
    "wellfound": _scrape_wellfound,
    "indeed": _scrape_indeed,
    "hirist": _scrape_hirist,
    "glassdoor": _scrape_glassdoor,
    "instahyre": _scrape_instahyre,
    "cutshort": _scrape_cutshort,
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def search_jobs(
    keywords: list[str] | None = None,
    location: str = "India",
    experience_years: int = 3,
    remote: bool = False,
    platforms: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Search job platforms concurrently and return merged results.
    If *platforms* is given, only search those (e.g. ["naukri"]).
    """
    if not keywords:
        keywords = list(PROFILE.default_search_keywords)

    kw_string = ", ".join(keywords)
    if remote:
        kw_string += " remote"

    # Determine which platforms to search
    active_platforms = (
        [p for p in platforms if p in PLATFORM_SCRAPERS]
        if platforms
        else list(PLATFORM_SCRAPERS.keys())
    )

    async with async_playwright() as pw:
        # Use Firefox — Chromium gets TLS-fingerprint blocked by many job sites
        browser = await pw.firefox.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) "
                "Gecko/20100101 Firefox/128.0"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            ignore_https_errors=True,
        )

        # Load any saved sessions
        for platform in active_platforms:
            await load_cookies(context, platform)

        # Build URLs
        urls: dict[str, str] = {}
        for platform in active_platforms:
            urls[platform] = PLATFORM_BUILDERS[platform](kw_string, location, experience_years)

        # Scrape concurrently — one page per platform
        async def _run(platform: str) -> list[JobResult]:
            page = await context.new_page()
            try:
                scraper = PLATFORM_SCRAPERS[platform]
                return await scraper(page, urls[platform])
            finally:
                await page.close()

        tasks = [_run(p) for p in active_platforms]
        platform_results = await asyncio.gather(*tasks, return_exceptions=True)

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
    return filtered[:20]
