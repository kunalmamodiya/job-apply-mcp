"""
apply_job   — automate a single application via Playwright.
bulk_apply  — iterate over a job list with delays, skip duplicates.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from playwright.async_api import Page, async_playwright

from config import AppConfig, load_config
from tools.session import load_cookies, save_cookies_from_context
from tools.tracker import is_already_applied, record_application

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Smart form auto-filler
# ---------------------------------------------------------------------------

async def _autofill_fields(page: Page, cfg: AppConfig) -> int:
    """
    Scan all visible input/select/textarea fields on the page and fill them
    using the autofill config.  Returns the number of fields filled.
    """
    af = cfg.autofill
    if not af:
        return 0

    exp_map: dict[str, str] = af.get("experience", {})
    pref_locs: list[str] = af.get("preferred_locations", [])

    # Build keyword -> value lookup for experience fields
    # Keys are lowered keywords; values are years strings
    exp_lookup = {k.lower(): v for k, v in exp_map.items()}

    filled = 0

    # --- Text / number inputs ---
    inputs = await page.query_selector_all(
        "input[type='text']:visible, input[type='number']:visible, "
        "input[type='tel']:visible, input:not([type]):visible, "
        "textarea:visible"
    )
    for inp in inputs:
        # Skip if already has a value
        try:
            current = (await inp.input_value()).strip()
            if current:
                continue
        except Exception:
            continue

        # Gather context: placeholder, label, name, aria-label
        placeholder = ((await inp.get_attribute("placeholder")) or "").lower()
        name_attr = ((await inp.get_attribute("name")) or "").lower()
        aria = ((await inp.get_attribute("aria-label")) or "").lower()
        # Try to find associated label
        inp_id = await inp.get_attribute("id")
        label_text = ""
        if inp_id:
            label_el = await page.query_selector(f"label[for='{inp_id}']")
            if label_el:
                label_text = (await label_el.inner_text()).lower()
        context = f"{placeholder} {name_attr} {aria} {label_text}"

        value = None

        # Name
        if any(w in context for w in ("name", "full name", "candidate name")):
            if "company" not in context and "job" not in context:
                value = cfg.name
        # Email
        elif any(w in context for w in ("email", "e-mail")):
            value = cfg.email
        # Phone
        elif any(w in context for w in ("phone", "mobile", "contact number")):
            value = cfg.phone
        # Date of birth
        elif any(w in context for w in ("dob", "date of birth", "birth date", "birthdate")):
            value = af.get("date_of_birth", "")
        # Gender
        elif "gender" in context:
            value = af.get("gender", "")
        # Notice period
        elif any(w in context for w in ("notice period", "notice_period", "noticePeriod")):
            value = af.get("notice_period", "")
        # CTC
        elif any(w in context for w in ("current ctc", "current_ctc", "currentctc", "current salary")):
            value = af.get("current_ctc", "")
        elif any(w in context for w in ("expected ctc", "expected_ctc", "expectedctc", "expected salary")):
            value = af.get("expected_ctc", "")
        # Total experience
        elif any(w in context for w in ("total experience", "total_experience", "totalexp", "overall experience", "years of experience")):
            value = af.get("total_experience", "")
        # Location / city
        elif any(w in context for w in ("location", "city", "preferred location", "current location")):
            value = pref_locs[0] if pref_locs else cfg.location
        else:
            # Try matching against experience keywords
            for kw, yrs in exp_lookup.items():
                kw_parts = kw.split()
                if all(p in context for p in kw_parts):
                    value = yrs
                    break

        if value:
            try:
                await inp.fill(str(value))
                filled += 1
            except Exception:
                pass

    # --- Select / dropdown fields ---
    selects = await page.query_selector_all("select:visible")
    for sel in selects:
        name_attr = ((await sel.get_attribute("name")) or "").lower()
        aria = ((await sel.get_attribute("aria-label")) or "").lower()
        sel_id = await sel.get_attribute("id")
        label_text = ""
        if sel_id:
            label_el = await page.query_selector(f"label[for='{sel_id}']")
            if label_el:
                label_text = (await label_el.inner_text()).lower()
        context = f"{name_attr} {aria} {label_text}"

        # Get all option texts
        options = await sel.query_selector_all("option")
        option_texts = []
        for opt in options:
            option_texts.append(((await opt.inner_text()).strip(), await opt.get_attribute("value") or ""))

        chosen = None

        if "gender" in context:
            target = af.get("gender", "male").lower()
            for text, val in option_texts:
                if target in text.lower():
                    chosen = val or text
                    break
        elif any(w in context for w in ("location", "city", "preferred")):
            for pref in pref_locs:
                for text, val in option_texts:
                    if pref.lower() in text.lower():
                        chosen = val or text
                        break
                if chosen:
                    break
        elif any(w in context for w in ("notice", "notice_period")):
            target = af.get("notice_period", "immediate").lower()
            for text, val in option_texts:
                if target in text.lower():
                    chosen = val or text
                    break
        elif any(w in context for w in ("experience", "exp")):
            # Match the closest experience value
            for text, val in option_texts:
                if "3" in text or "4" in text:
                    chosen = val or text
                    break

        if chosen:
            try:
                await sel.select_option(chosen)
                filled += 1
            except Exception:
                pass

    # --- Radio buttons ---
    # Handle gender, location preference, etc.
    radios = await page.query_selector_all("input[type='radio']:visible")
    radio_groups: dict[str, list] = {}
    for radio in radios:
        rname = await radio.get_attribute("name") or ""
        if rname not in radio_groups:
            radio_groups[rname] = []
        label_el = await radio.evaluate_handle(
            "el => el.closest('label') || el.parentElement"
        )
        label_text = ""
        try:
            label_text = (await label_el.as_element().inner_text()).lower()
        except Exception:
            pass
        rvalue = (await radio.get_attribute("value") or "").lower()
        radio_groups[rname].append((radio, rvalue, label_text))

    for rname, group in radio_groups.items():
        context = rname.lower()
        target = None
        if "gender" in context:
            target = af.get("gender", "male").lower()
        elif any(w in context for w in ("location", "city", "remote")):
            target = pref_locs[0].lower() if pref_locs else None

        if target:
            for radio, rvalue, label_text in group:
                if target in rvalue or target in label_text:
                    try:
                        await radio.check()
                        filled += 1
                    except Exception:
                        pass
                    break

    if filled:
        logger.info("Autofill: filled %d fields", filled)
    return filled


# ---------------------------------------------------------------------------
# Resume upload helper
# ---------------------------------------------------------------------------

async def _upload_resume(page: Page, resume_path: str) -> bool:
    """
    Find any file-upload input on the page and upload the resume.
    Handles visible inputs, hidden inputs, and inputs inside shadow DOM.
    Returns True if a file was uploaded.
    """
    # Strategy 1: visible file input
    file_inputs = await page.query_selector_all("input[type='file']")
    for fi in file_inputs:
        try:
            await fi.set_input_files(resume_path)
            logger.info("Resume uploaded via visible file input")
            await page.wait_for_timeout(1000)
            return True
        except Exception:
            pass

    # Strategy 2: click an "Upload Resume" / "Attach Resume" button/link,
    # which may reveal a hidden file input via JS
    upload_btn = await page.query_selector(
        "button:has-text('Upload'), button:has-text('Attach'), "
        "a:has-text('Upload Resume'), a:has-text('Attach Resume'), "
        "label:has-text('Upload'), label:has-text('Attach'), "
        "div[class*='upload']:has-text('Upload'), "
        "span:has-text('Upload Resume')"
    )
    if upload_btn:
        # Set up a file chooser listener before clicking
        try:
            async with page.expect_file_chooser(timeout=5000) as fc_info:
                await upload_btn.click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(resume_path)
            logger.info("Resume uploaded via file chooser dialog")
            await page.wait_for_timeout(1000)
            return True
        except Exception:
            pass

    # Strategy 3: hidden file input revealed after page interaction
    hidden_inputs = await page.evaluate('''() => {
        const inputs = document.querySelectorAll('input[type="file"]');
        return inputs.length;
    }''')
    if hidden_inputs > 0:
        try:
            # Force the input to be interactable
            await page.evaluate('''() => {
                const inp = document.querySelector('input[type="file"]');
                if (inp) {
                    inp.style.display = "block";
                    inp.style.visibility = "visible";
                    inp.style.opacity = "1";
                }
            }''')
            fi = await page.query_selector("input[type='file']")
            if fi:
                await fi.set_input_files(resume_path)
                logger.info("Resume uploaded via unhidden file input")
                await page.wait_for_timeout(1000)
                return True
        except Exception:
            pass

    return False


# ---------------------------------------------------------------------------
# CAPTCHA detection helper
# ---------------------------------------------------------------------------

async def _detect_captcha(page: Page) -> bool:
    """Check VISIBLE page text for CAPTCHA indicators (avoids false positives from scripts)."""
    try:
        text = (await page.inner_text("body")).lower()
    except Exception:
        text = (await page.content()).lower()
    indicators = ("recaptcha", "hcaptcha", "cf-challenge", "verify you are human")
    return any(ind in text for ind in indicators)


# ---------------------------------------------------------------------------
# Per-platform apply helpers
# ---------------------------------------------------------------------------

async def _apply_linkedin(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Attempt to apply via LinkedIn Easy Apply."""
    try:
        easy_apply_btn = await page.query_selector(
            "button.jobs-apply-button, button[data-control-name='jobdetails_topcard_inapply']"
        )
        if not easy_apply_btn:
            return {"success": False, "error": "No Easy Apply button found — external application required"}

        await easy_apply_btn.click()
        await page.wait_for_timeout(1500)

        # Fill phone if present and empty
        phone_input = await page.query_selector("input[name*='phone'], input[id*='phone']")
        if phone_input:
            current = await phone_input.input_value()
            if not current.strip():
                await phone_input.fill(cfg.phone)

        # Upload resume if upload button exists
        file_input = await page.query_selector("input[type='file']")
        if file_input and cfg.resume_exists:
            await file_input.set_input_files(cfg.resume_path)
            await page.wait_for_timeout(1000)

        # Add cover note in additional questions textarea
        if cover_note:
            textarea = await page.query_selector("textarea[name*='cover'], textarea[name*='additional']")
            if textarea:
                await textarea.fill(cover_note)

        # Try to click through multi-step form
        for _ in range(5):
            submit_btn = await page.query_selector(
                "button[aria-label='Submit application'], button[aria-label='Review']"
            )
            next_btn = await page.query_selector(
                "button[aria-label='Continue to next step'], button[aria-label='Next']"
            )
            if submit_btn:
                await submit_btn.click()
                await page.wait_for_timeout(2000)
                return {"success": True, "confirmation": "LinkedIn Easy Apply submitted"}
            elif next_btn:
                await next_btn.click()
                await page.wait_for_timeout(1000)
            else:
                break

        return {"success": False, "error": "Could not complete LinkedIn application flow"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _naukri_login(page: Page, cfg: AppConfig) -> bool:
    """Log in to Naukri inline if not already authenticated."""
    creds = cfg.credentials.get("naukri", {})
    email = creds.get("email", "")
    password = creds.get("password", "")
    if not email or not password:
        return False

    # Click "Login to apply" if visible
    login_btn = await page.query_selector("button#login-apply-button")
    if not login_btn or not await login_btn.is_visible():
        return True  # already logged in

    await login_btn.click()
    await page.wait_for_timeout(2000)

    # Fill login form
    email_input = await page.query_selector(
        "input[type='email'], input[placeholder*='Email'], input[id*='usernameField']"
    )
    pass_input = await page.query_selector(
        "input[type='password'], input[placeholder*='Password'], input[id*='passwordField']"
    )
    if email_input and pass_input:
        await email_input.fill(email)
        await pass_input.fill(password)
        submit = await page.query_selector(
            "button[type='submit'], button[class*='loginButton'], button:has-text('Login')"
        )
        if submit:
            await submit.click()
            await page.wait_for_timeout(4000)
            logger.info("Naukri: login submitted")
    return True


async def _apply_naukri(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Naukri.com."""
    try:
        # Check if logged in — if "Login to apply" is visible, log in first
        login_btn = await page.query_selector("button#login-apply-button")
        if login_btn and await login_btn.is_visible():
            logged_in = await _naukri_login(page, cfg)
            if not logged_in:
                return {"success": False, "error": "Naukri login failed — check credentials in config.json"}
            # Reload the job page after login
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

        # Check if this is an external-apply job — skip without clicking
        is_external = await page.evaluate('''() => {
            const extBtn = document.querySelector('button#company-site-button');
            return !!(extBtn && extBtn.offsetParent !== null);
        }''')
        if is_external:
            return {"success": False, "error": "External apply (company site) — skipped"}

        # Click the direct Apply button only
        clicked = await page.evaluate('''() => {
            const btn = document.querySelector('button#apply-button, button.apply-button');
            if (btn) {
                btn.scrollIntoView();
                btn.click();
                return true;
            }
            return false;
        }''')

        if not clicked:
            return {"success": False, "error": "No direct apply button found"}

        await page.wait_for_timeout(3000)

        # --- Multi-step apply loop ---
        # Naukri may show chatbot questions, resume upload, or a multi-step
        # form.  We loop up to 10 rounds: each round we autofill fields,
        # upload resume if asked, and click the next/submit button.
        filled_total = 0
        for step in range(10):
            # 1) Auto-fill any visible form fields
            filled_total += await _autofill_fields(page, cfg)

            # 2) Upload resume — check ALL file inputs (visible + hidden)
            if cfg.resume_exists:
                await _upload_resume(page, cfg.resume_path)

            # 3) Check if we're done
            text = (await page.inner_text("body")).lower()
            if "already applied" in text:
                return {"success": True, "confirmation": "Already applied to this job on Naukri"}
            if "application submitted" in text or "applied successfully" in text:
                return {"success": True, "confirmation": "Naukri application submitted successfully"}

            # 4) Find and click submit/next/continue button
            clicked = await page.evaluate('''() => {
                // Priority order: Submit > Next > Continue > any chatbot button
                const selectors = [
                    'button[class*="chatbot_SubmitBtn"]',
                    'button[class*="submit"]:not([disabled])',
                    'button[type="submit"]:not([disabled])',
                    'button:has-text("Submit"):not([disabled])',
                    'button:has-text("Next"):not([disabled])',
                    'button:has-text("Continue"):not([disabled])',
                    'button:has-text("Apply"):not([disabled])',
                ];
                for (const sel of selectors) {
                    try {
                        const btn = document.querySelector(sel);
                        if (btn && btn.offsetParent !== null) {
                            btn.scrollIntoView();
                            btn.click();
                            return sel;
                        }
                    } catch(e) {}
                }
                return "";
            }''')

            if not clicked:
                break  # No more buttons to click — we're done

            logger.info("Naukri step %d: clicked %s", step + 1, clicked)
            await page.wait_for_timeout(2500)

        # Final check
        text = (await page.inner_text("body")).lower()
        if "already applied" in text:
            return {"success": True, "confirmation": "Already applied to this job on Naukri"}
        if "application submitted" in text or "applied successfully" in text:
            return {"success": True, "confirmation": "Naukri application submitted successfully"}

        return {"success": True, "confirmation": f"Naukri apply completed (autofilled {filled_total} fields)"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _apply_wellfound(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Wellfound."""
    try:
        apply_btn = await page.query_selector(
            "button[data-test='apply-button'], button[class*='apply'], a[class*='apply']"
        )
        if not apply_btn:
            return {"success": False, "error": "Apply button not found on Wellfound"}

        await apply_btn.click()
        await page.wait_for_timeout(2000)

        # Cover note
        if cover_note:
            textarea = await page.query_selector(
                "textarea[name*='cover'], textarea[placeholder*='cover'], textarea[data-test='cover-letter']"
            )
            if textarea:
                await textarea.fill(cover_note)

        # Upload resume
        file_input = await page.query_selector("input[type='file']")
        if file_input and cfg.resume_exists:
            await file_input.set_input_files(cfg.resume_path)
            await page.wait_for_timeout(1000)

        # Submit
        submit_btn = await page.query_selector(
            "button[type='submit'], button[data-test='submit-application']"
        )
        if submit_btn:
            await submit_btn.click()
            await page.wait_for_timeout(2000)

        return {"success": True, "confirmation": "Wellfound application submitted"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _apply_indeed(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Indeed."""
    try:
        apply_btn = await page.query_selector(
            "button#indeedApplyButton, button[class*='apply'], a[class*='apply']"
        )
        if not apply_btn:
            return {"success": False, "error": "Apply button not found on Indeed"}

        await apply_btn.click()
        await page.wait_for_timeout(2000)

        # Fill name
        name_input = await page.query_selector("input[name*='name'], input[id*='name']")
        if name_input:
            current = await name_input.input_value()
            if not current.strip():
                await name_input.fill(cfg.name)

        # Fill email
        email_input = await page.query_selector("input[name*='email'], input[type='email']")
        if email_input:
            current = await email_input.input_value()
            if not current.strip():
                await email_input.fill(cfg.email)

        # Fill phone
        phone_input = await page.query_selector("input[name*='phone'], input[id*='phone']")
        if phone_input:
            current = await phone_input.input_value()
            if not current.strip():
                await phone_input.fill(cfg.phone)

        # Upload resume
        file_input = await page.query_selector("input[type='file']")
        if file_input and cfg.resume_exists:
            await file_input.set_input_files(cfg.resume_path)
            await page.wait_for_timeout(1000)

        # Continue / Submit
        for _ in range(5):
            cont_btn = await page.query_selector(
                "button[id*='continue'], button[class*='continue'], button[type='submit']"
            )
            if cont_btn:
                label = (await cont_btn.inner_text()).lower()
                await cont_btn.click()
                await page.wait_for_timeout(1500)
                if "submit" in label:
                    return {"success": True, "confirmation": "Indeed application submitted"}
            else:
                break

        return {"success": True, "confirmation": "Indeed application flow completed"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _apply_hirist(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Hirist.tech."""
    try:
        apply_btn = await page.query_selector(
            "button.apply-btn, button[class*='apply'], a.apply-btn"
        )
        if not apply_btn:
            return {"success": False, "error": "Apply button not found on Hirist"}

        await apply_btn.click()
        await page.wait_for_timeout(2000)

        # Upload resume
        file_input = await page.query_selector("input[type='file']")
        if file_input and cfg.resume_exists:
            await file_input.set_input_files(cfg.resume_path)
            await page.wait_for_timeout(1000)

        # Submit
        submit_btn = await page.query_selector("button[type='submit'], button.submit")
        if submit_btn:
            await submit_btn.click()
            await page.wait_for_timeout(2000)

        return {"success": True, "confirmation": "Hirist application submitted"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _apply_glassdoor(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Glassdoor."""
    try:
        # Glassdoor has "Easy Apply" and "Apply on employer site"
        # Only do Easy Apply
        easy_btn = await page.query_selector(
            "button[data-test='applyButton']:has-text('Easy Apply'), "
            "button[class*='EasyApply'], "
            "button:has-text('Easy Apply')"
        )
        if not easy_btn:
            # Check if it's an external apply
            ext_btn = await page.query_selector(
                "button:has-text('Apply on employer site'), "
                "a:has-text('Apply on employer site')"
            )
            if ext_btn:
                return {"success": False, "error": "External apply (employer site) — skipped"}
            return {"success": False, "error": "No Easy Apply button found on Glassdoor"}

        await easy_btn.click()
        await page.wait_for_timeout(3000)

        # Auto-fill form fields
        filled = await _autofill_fields(page, cfg)

        # Upload resume if prompted
        if cfg.resume_exists:
            await _upload_resume(page, cfg.resume_path)

        # Multi-step: click Next/Submit up to 5 times
        for _ in range(5):
            text = (await page.inner_text("body")).lower()
            if "application submitted" in text or "applied" in text:
                return {"success": True, "confirmation": "Glassdoor Easy Apply submitted"}

            await _autofill_fields(page, cfg)
            if cfg.resume_exists:
                await _upload_resume(page, cfg.resume_path)

            submit = await page.query_selector(
                "button:has-text('Submit'), button:has-text('Next'), "
                "button:has-text('Continue'), button[type='submit']"
            )
            if submit and await submit.is_visible():
                await submit.click()
                await page.wait_for_timeout(2500)
            else:
                break

        return {"success": True, "confirmation": f"Glassdoor apply completed (autofilled {filled} fields)"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _apply_instahyre(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Instahyre — mostly one-click 'Apply' or 'Interested'."""
    try:
        apply_btn = await page.query_selector(
            "button:has-text('Apply'), button:has-text('Interested'), "
            "button[class*='apply'], a[class*='apply'], "
            "button:has-text('I am interested')"
        )
        if not apply_btn:
            return {"success": False, "error": "Apply button not found on Instahyre"}

        await apply_btn.click()
        await page.wait_for_timeout(3000)

        # Auto-fill any form that appears
        filled = await _autofill_fields(page, cfg)

        # Upload resume if prompted
        if cfg.resume_exists:
            await _upload_resume(page, cfg.resume_path)

        # Click submit/confirm if present
        for _ in range(3):
            submit = await page.query_selector(
                "button:has-text('Submit'), button:has-text('Confirm'), "
                "button:has-text('Apply'), button[type='submit']"
            )
            if submit and await submit.is_visible():
                await submit.click()
                await page.wait_for_timeout(2000)
                await _autofill_fields(page, cfg)
            else:
                break

        text = (await page.inner_text("body")).lower()
        if "applied" in text or "application" in text or "interested" in text:
            return {"success": True, "confirmation": "Instahyre application submitted"}

        return {"success": True, "confirmation": f"Instahyre apply completed (autofilled {filled} fields)"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


async def _apply_cutshort(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply on Cutshort — one-click 'Apply' with optional questions."""
    try:
        apply_btn = await page.query_selector(
            "button:has-text('Apply'), button[class*='apply'], "
            "a:has-text('Apply'), button:has-text('I\\'m interested')"
        )
        if not apply_btn:
            return {"success": False, "error": "Apply button not found on Cutshort"}

        await apply_btn.click()
        await page.wait_for_timeout(3000)

        # Auto-fill any form that appears
        filled = await _autofill_fields(page, cfg)

        # Upload resume if prompted
        if cfg.resume_exists:
            await _upload_resume(page, cfg.resume_path)

        # Multi-step: submit through any questionnaire
        for _ in range(5):
            text = (await page.inner_text("body")).lower()
            if "applied" in text or "application submitted" in text:
                return {"success": True, "confirmation": "Cutshort application submitted"}

            await _autofill_fields(page, cfg)
            if cfg.resume_exists:
                await _upload_resume(page, cfg.resume_path)

            submit = await page.query_selector(
                "button:has-text('Submit'), button:has-text('Next'), "
                "button:has-text('Apply'), button[type='submit']"
            )
            if submit and await submit.is_visible():
                await submit.click()
                await page.wait_for_timeout(2500)
            else:
                break

        return {"success": True, "confirmation": f"Cutshort apply completed (autofilled {filled} fields)"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


PLATFORM_APPLYERS = {
    "linkedin": _apply_linkedin,
    "naukri": _apply_naukri,
    "wellfound": _apply_wellfound,
    "indeed": _apply_indeed,
    "hirist": _apply_hirist,
    "glassdoor": _apply_glassdoor,
    "instahyre": _apply_instahyre,
    "cutshort": _apply_cutshort,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def apply_job(
    job_url: str,
    platform: str,
    cover_note: str = "",
    job_title: str = "",
    company: str = "",
    match_score: float = 0.0,
) -> dict[str, Any]:
    """
    Automate a single job application.
    Returns {success, error?, confirmation?}.
    """
    platform = platform.lower().strip()
    if platform not in PLATFORM_APPLYERS:
        return {"success": False, "error": f"Unsupported platform: {platform}"}

    cfg = load_config()
    if not cfg.resume_exists:
        return {
            "success": False,
            "error": "Resume file not found. Set 'resume_path' in ~/.job-apply-mcp/config.json",
        }

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
        await load_cookies(context, platform)

        page = await context.new_page()
        await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3000)

        if await _detect_captcha(page):
            await browser.close()
            return {
                "success": False,
                "error": (
                    f"CAPTCHA detected on {platform}. "
                    "Please run save_session to log in manually, then retry."
                ),
                "captcha": True,
            }

        applyer = PLATFORM_APPLYERS[platform]
        result = await applyer(page, cfg, cover_note)

        # Save cookies after apply (captures post-login session)
        try:
            await save_cookies_from_context(context, platform)
        except Exception:
            pass

        await browser.close()

    # Track in DB
    status = "applied" if result.get("success") else "failed"
    try:
        record_application(
            job_title=job_title or "Unknown",
            company=company or "Unknown",
            platform=platform,
            job_url=job_url,
            status=status,
            confirmation=result.get("confirmation"),
            cover_note=cover_note or None,
            match_score=match_score,
        )
    except Exception as exc:
        logger.warning("Failed to record application: %s", exc)

    return result


async def _apply_in_tab(
    context,
    job_url: str,
    platform: str,
    cfg: AppConfig,
    cover_note: str = "",
) -> dict[str, Any]:
    """Apply to a single job using a new tab in an existing browser context."""
    page = await context.new_page()
    try:
        await page.goto(job_url, wait_until="domcontentloaded", timeout=25_000)
        await page.wait_for_timeout(2000)

        if await _detect_captcha(page):
            return {"success": False, "error": "CAPTCHA detected", "captcha": True}

        applyer = PLATFORM_APPLYERS.get(platform)
        if not applyer:
            return {"success": False, "error": f"Unsupported platform: {platform}"}

        return await applyer(page, cfg, cover_note)
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        await page.close()


async def bulk_apply(
    jobs: list[dict[str, Any]],
    max_applications: int = 10,
    dry_run: bool = True,
) -> dict[str, Any]:
    """
    Apply to multiple jobs using a SINGLE browser session for speed.
    Delay between applications is 5-15 seconds.
    """
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    # Pre-filter before launching browser
    to_apply: list[dict[str, Any]] = []
    for job in jobs:
        if len(to_apply) >= max_applications:
            break
        url = job.get("apply_url", "")
        platform = job.get("platform", "")
        if not url or not platform:
            skipped.append({**job, "reason": "Missing URL or platform"})
            continue
        if is_already_applied(url):
            skipped.append({**job, "reason": "Already applied"})
            continue
        if dry_run:
            applied.append({**job, "dry_run": True})
            continue
        to_apply.append(job)

    if not to_apply or dry_run:
        # Nothing to actually apply to, or dry run already handled above
        pass
    else:
        cfg = load_config()
        if not cfg.resume_exists:
            return {
                "summary": {"error": "Resume not found"},
                "applied": [], "skipped": skipped, "failed": [],
            }

        # One browser, one context, reuse for all jobs
        async with async_playwright() as pw:
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
            # Load session cookies once
            platforms_loaded = set()
            for job in to_apply:
                p = job.get("platform", "")
                if p and p not in platforms_loaded:
                    await load_cookies(context, p)
                    platforms_loaded.add(p)

            for i, job in enumerate(to_apply):
                url = job["apply_url"]
                platform = job["platform"]
                title = job.get("title", "Unknown")
                company = job.get("company", "Unknown")

                logger.info("[%d/%d] Applying: %s @ %s", i + 1, len(to_apply), title, company)

                result = await _apply_in_tab(
                    context, url, platform, cfg, job.get("cover_note", ""),
                )

                # Track in DB
                status = "applied" if result.get("success") else "failed"
                try:
                    record_application(
                        job_title=title, company=company, platform=platform,
                        job_url=url, status=status,
                        confirmation=result.get("confirmation"),
                        cover_note=job.get("cover_note") or None,
                        match_score=job.get("match_score", 0),
                    )
                except Exception:
                    pass

                if result.get("success"):
                    applied.append({**job, **result})
                else:
                    failed.append({**job, **result})

                # Short delay — 5-15 seconds
                if i < len(to_apply) - 1:
                    delay = random.uniform(5, 15)
                    await asyncio.sleep(delay)

            # Save cookies once at end
            for p in platforms_loaded:
                try:
                    await save_cookies_from_context(context, p)
                except Exception:
                    pass

            await browser.close()

    return {
        "summary": {
            "total_processed": len(applied) + len(skipped) + len(failed),
            "applied": len(applied),
            "skipped": len(skipped),
            "failed": len(failed),
            "dry_run": dry_run,
        },
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
    }
