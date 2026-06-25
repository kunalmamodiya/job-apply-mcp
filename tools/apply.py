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

from config import AppConfig, get_user_agent, load_config
from tools.session import load_cookies, save_cookies_from_context
from tools.tracker import is_already_applied, record_application

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Smart form auto-filler
# ---------------------------------------------------------------------------

async def _autofill_fields(page: Page, cfg: AppConfig, job_location: str = "") -> int:
    """
    Scan all visible input/select/textarea fields on the page and fill them
    using the autofill config via JavaScript for reliability.
    Returns the number of fields filled.
    """
    af = cfg.autofill
    if not af:
        return 0

    current_city = (cfg.location or "").split(",")[0].strip()
    job_city = (job_location or "").split(",")[0].strip()
    preferred_city = job_city or current_city

    # Build the full answer map to pass into JS
    # ORDER MATTERS: more specific keys first so they match before shorter keys.
    answers: dict[str, str] = {
        "name": cfg.name,
        "email": cfg.email,
        "phone": cfg.phone,
        "gender": af.get("gender", ""),
        "date_of_birth": af.get("date_of_birth", ""),
        "dob": af.get("date_of_birth", ""),
        "notice_period": af.get("notice_period", ""),
        "notice period": af.get("notice_period", ""),
        "current_ctc": af.get("current_ctc", ""),
        "current ctc": af.get("current_ctc", ""),
        "current salary": af.get("current_ctc", ""),
        "expected_ctc": af.get("expected_ctc", ""),
        "expected ctc": af.get("expected_ctc", ""),
        "expected salary": af.get("expected_ctc", ""),
        "total_experience": af.get("total_experience", ""),
        "total experience": af.get("total_experience", ""),
        "years of experience": af.get("total_experience", ""),
        "overall experience": af.get("total_experience", ""),
        # Relocation — answer Yes
        "willing to relocate": "Yes",
        "open to relocate": "Yes",
        "open to relocation": "Yes",
        "relocate": "Yes",
        # Location — current vs. preferred (preferred is JD-aware, falls back to current)
        "current location": current_city,
        "current city": current_city,
        "present location": current_city,
        "home location": current_city,
        "where are you based": current_city,
        "preferred location": preferred_city,
        "preferred city": preferred_city,
        "location": current_city,
        "city": current_city,
        "certifications": af.get("certifications", ""),
        "certification": af.get("certifications", ""),
        "certificate": af.get("certifications", ""),
        "certificates": af.get("certifications", ""),
        "certified": af.get("certifications", ""),
        "military spouse": "No",
        "military partner": "No",
    }
    # Add experience keywords
    for k, v in af.get("experience", {}).items():
        answers[k.lower()] = v

    pref_locs = af.get("preferred_locations", [])

    # ---- Use JavaScript to find and fill all visible fields ----
    filled = await page.evaluate(r'''(config) => {
        const answers = config.answers;
        const prefLocs = config.prefLocs;
        let filled = 0;

        function getContext(el) {
            const ph = (el.placeholder || "").toLowerCase();
            const nm = (el.name || "").toLowerCase();
            const ar = (el.getAttribute("aria-label") || "").toLowerCase();
            const id = el.id || "";
            let lbl = "";
            if (id) {
                const labelEl = document.querySelector('label[for="' + id + '"]');
                if (labelEl) lbl = labelEl.textContent.toLowerCase();
            }
            // Also check parent/sibling text
            const parent = el.closest("div, li, td, span, label");
            const parentText = parent ? parent.textContent.toLowerCase().substring(0, 200) : "";
            return (ph + " " + nm + " " + ar + " " + lbl + " " + parentText).toLowerCase();
        }

        function isVisible(el) {
            return el.offsetParent !== null || el.offsetWidth > 0 || el.offsetHeight > 0;
        }

        function triggerChange(el) {
            el.dispatchEvent(new Event("input", {bubbles: true}));
            el.dispatchEvent(new Event("change", {bubbles: true}));
        }

        // --- Text/number/tel inputs and textareas ---
        const inputs = document.querySelectorAll(
            'input[type="text"], input[type="number"], input[type="tel"], ' +
            'input:not([type]), textarea'
        );
        for (const inp of inputs) {
            if (!isVisible(inp)) continue;
            if (inp.value && inp.value.trim()) continue;
            // Skip search bars
            if (inp.placeholder && /search|keyword/i.test(inp.placeholder)) continue;

            const ctx = getContext(inp);
            let matched = false;

            // Try each answer key against the context
            for (const [key, val] of Object.entries(answers)) {
                if (!val) continue;
                const parts = key.split(/\s+/);
                if (parts.every(p => ctx.includes(p))) {
                    // Extra guard: don't fill "company name" with person name
                    if (key === "name" && (ctx.includes("company") || ctx.includes("job"))) continue;
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set || Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    )?.set;
                    if (nativeSetter) {
                        nativeSetter.call(inp, val);
                    } else {
                        inp.value = val;
                    }
                    triggerChange(inp);
                    filled++;
                    matched = true;
                    break;
                }
            }
        }

        // --- Select dropdowns ---
        const selects = document.querySelectorAll("select");
        for (const sel of selects) {
            if (!isVisible(sel)) continue;
            const ctx = getContext(sel);
            const options = Array.from(sel.options).map(o => ({
                text: o.textContent.toLowerCase().trim(),
                value: o.value
            }));

            let chosen = null;

            if (ctx.includes("gender")) {
                const target = (answers.gender || "male").toLowerCase();
                chosen = options.find(o => o.text.includes(target));
            } else if (/relocat|willing to move|shift to/.test(ctx)) {
                chosen = options.find(o => /^yes$|\byes\b/i.test(o.text));
            } else if (/current.*(location|city)|present.*(location|city)|home.*(location|city)|where.*based|residing/.test(ctx)) {
                if (config.currentCity) chosen = options.find(o => o.text.includes(config.currentCity.toLowerCase()));
            } else if (ctx.includes("location") || ctx.includes("city") || ctx.includes("preferred")) {
                if (config.preferredCity) chosen = options.find(o => o.text.includes(config.preferredCity.toLowerCase()));
                if (!chosen) {
                    for (const pref of prefLocs) {
                        chosen = options.find(o => o.text.includes(pref.toLowerCase()));
                        if (chosen) break;
                    }
                }
            } else if (ctx.includes("notice")) {
                const target = (answers.notice_period || "immediate").toLowerCase();
                chosen = options.find(o => o.text.includes(target));
            } else if (ctx.includes("experience") || ctx.includes("exp")) {
                const candidates = options.filter(o => !/fresher|no experience|\b0\b/i.test(o.text));
                chosen = candidates.find(o => /\b4\b|3.*5|3\s*-\s*5|3\s*to\s*5/.test(o.text)) ||
                         candidates.find(o => /[34]/.test(o.text)) ||
                         candidates[0] || options[0];
            } else if (/certified|certificat|certification|\bcert\b/.test(ctx)) {
                chosen = options.find(o => /^yes$|\byes\b/i.test(o.text));
            } else if (/mil.*spouse|mil.*partner/.test(ctx)) {
                chosen = options.find(o => /not\s*a\s*military|non-military|not\s*a\s*spouse|no\b|not\b/i.test(o.text));
            }

            if (chosen) {
                sel.value = chosen.value;
                triggerChange(sel);
                filled++;
            }
        }

        // --- Radio buttons ---
        const radios = document.querySelectorAll('input[type="radio"]');
        const radioGroups = {};
        for (const r of radios) {
            if (!isVisible(r)) continue;
            const name = r.name || "";
            if (!radioGroups[name]) radioGroups[name] = [];
            const label = (r.closest("label") || r.parentElement);
            const labelText = label ? label.textContent.toLowerCase().trim() : "";
            radioGroups[name].push({el: r, value: (r.value || "").toLowerCase(), label: labelText});
        }
        for (const [name, group] of Object.entries(radioGroups)) {
            const ctx = name.toLowerCase();
            let match = null;

            if (/relocat|willing to move|shift to/.test(ctx)) {
                match = group.find(r => /^yes$|\byes\b/i.test(r.label) || /^yes$|\byes\b/i.test(r.value));
            } else if (ctx.includes("gender")) {
                const target = (answers.gender || "male").toLowerCase();
                match = group.find(r => r.value.includes(target) || r.label.includes(target));
            } else if (/certified|certificat|certification|\bcert\b/.test(ctx)) {
                match = group.find(r => /^yes$|\byes\b/i.test(r.label) || /^yes$|\byes\b/i.test(r.value));
            } else if (/mil.*spouse|mil.*partner/.test(ctx)) {
                match = group.find(r => /not\s*a\s*military|non-military|not\s*a\s*spouse|no\b|not\b/i.test(r.label) || /not\s*a\s*military|non-military|not\s*a\s*spouse|no\b|not\b/i.test(r.value));
            } else if (/current|present|home/.test(ctx) && (ctx.includes("location") || ctx.includes("city"))) {
                const target = (config.currentCity || "").toLowerCase();
                if (target) match = group.find(r => r.value.includes(target) || r.label.includes(target));
            } else if (ctx.includes("location") || ctx.includes("city") || ctx.includes("remote")) {
                const target = (config.preferredCity || prefLocs[0] || "").toLowerCase();
                if (target) match = group.find(r => r.value.includes(target) || r.label.includes(target));
            } else if (/experience|years|exp/.test(ctx)) {
                const candidates = group.filter(r => !/no experience|fresher|\b0\b|\bzero\b/i.test(r.label) && !/no experience|fresher|\b0\b|\bzero\b/i.test(r.value));
                match = candidates.find(r => /\b4\b|3.*5|3\s*-\s*5|3\s*to\s*5/.test(r.label) || /\b4\b|3.*5|3\s*-\s*5|3\s*to\s*5/.test(r.value)) ||
                        candidates.find(r => /[34]/.test(r.label) || /[34]/.test(r.value)) ||
                        candidates[0] || group[0];
            }

            if (match) {
                match.el.checked = true;
                match.el.dispatchEvent(new Event("change", {bubbles: true}));
                filled++;
            }
        }

        return filled;
    }''', {"answers": answers, "prefLocs": pref_locs, "currentCity": current_city, "preferredCity": preferred_city})

    if filled:
        logger.info("Autofill: filled %d fields", filled)
    return filled


# ---------------------------------------------------------------------------
# Resume upload helper
# ---------------------------------------------------------------------------

async def _upload_resume(page: Page, resume_path: str) -> bool:
    """
    Find any file-upload input on the page and upload the resume.
    Uses multiple strategies for maximum compatibility.
    Returns True if a file was uploaded.
    """
    # Strategy 1: Make ALL file inputs fully visible and interactable
    count = await page.evaluate('''() => {
        const inputs = document.querySelectorAll('input[type="file"]');
        for (const inp of inputs) {
            inp.style.cssText = "display:block !important; visibility:visible !important; opacity:1 !important; width:100px !important; height:30px !important; position:relative !important; z-index:99999 !important;";
            // Also make parents visible
            let parent = inp.parentElement;
            for (let i = 0; i < 5 && parent; i++) {
                parent.style.overflow = "visible";
                parent.style.display = "block";
                parent = parent.parentElement;
            }
        }
        return inputs.length;
    }''')

    if count > 0:
        # Try page.set_input_files with selector (more reliable than element handle)
        try:
            await page.set_input_files("input[type='file']", resume_path)
            logger.info("Resume uploaded via page.set_input_files")
            await page.wait_for_timeout(2000)
            return True
        except Exception as e:
            logger.debug("page.set_input_files failed: %s", e)

        # Fallback: try each file input element
        file_inputs = await page.query_selector_all("input[type='file']")
        for fi in file_inputs:
            try:
                await fi.set_input_files(resume_path)
                logger.info("Resume uploaded via element.set_input_files")
                await page.wait_for_timeout(2000)
                return True
            except Exception as e:
                logger.debug("element.set_input_files failed: %s", e)

    # Strategy 2: Click upload button/label and intercept file chooser
    upload_selectors = [
        "button:has-text('Upload')", "button:has-text('Attach')",
        "a:has-text('Upload Resume')", "a:has-text('Attach Resume')",
        "label:has-text('Upload')", "label:has-text('Attach')",
        "span:has-text('Upload Resume')", "span:has-text('upload resume')",
        "div:has-text('Upload Resume')",
        "label[for*='file']", "label[for*='resume']", "label[for*='upload']",
    ]
    for sel in upload_selectors:
        btn = await page.query_selector(sel)
        if btn:
            try:
                visible = await btn.is_visible()
                if not visible:
                    continue
                async with page.expect_file_chooser(timeout=5000) as fc_info:
                    await btn.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(resume_path)
                logger.info("Resume uploaded via file chooser (%s)", sel)
                await page.wait_for_timeout(2000)
                return True
            except Exception:
                pass

    # Strategy 3: Click ANY element that mentions upload/resume and intercept
    try:
        upload_el = await page.evaluate('''() => {
            const els = document.querySelectorAll('*');
            for (const el of els) {
                if (el.offsetParent === null) continue;
                const t = el.textContent.trim().toLowerCase();
                if (t.length < 50 && (t.includes('upload resume') || t.includes('attach resume') || t === 'upload' || t === 'attach')) {
                    return true;
                }
            }
            return false;
        }''')
        if upload_el:
            async with page.expect_file_chooser(timeout=5000) as fc_info:
                await page.click("text=/upload|attach/i")
            file_chooser = await fc_info.value
            await file_chooser.set_files(resume_path)
            logger.info("Resume uploaded via text-match click")
            await page.wait_for_timeout(2000)
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

        # Extract the job's location from the page so we can answer
        # "preferred location" questions with the JD's city, not "Remote".
        job_location = await page.evaluate("""() => {
            const sels = [
                'span.location', 'div.location', 'div.loc',
                '[class*="locationsContainer"] span', '[class*="locationsContainer"]',
                '[class*="location"] span', '[class*="location"]'
            ];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el && el.textContent.trim()) return el.textContent.trim();
            }
            return '';
        }""")
        job_city = (job_location or "").split(",")[0].split("/")[0].split("(")[0].strip()
        current_city = (cfg.location or "").split(",")[0].strip()
        preferred_city = job_city or current_city
        logger.info("Locations: current=%s preferred=%s (jd=%s)", current_city, preferred_city, job_location[:60])

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

        # --- Naukri chatbot / multi-step apply loop ---
        # Naukri chatbot uses:
        #   - contenteditable div (class="textArea") for text answers (NOT <input>)
        #   - radio buttons (class="ssrc__radio") for choice answers
        #   - div.sendMsg "Save" (NOT <button>) to submit each answer
        #   - input.chatbot_Uploader[type=file] for resume upload
        af = cfg.autofill or {}
        exp_map = {k.lower(): v for k, v in af.get("experience", {}).items()}
        answered = 0

        for step in range(15):
            await page.wait_for_timeout(2000)

            # Check if we're done — multiple success indicators
            text = (await page.inner_text("body")).lower()
            if "already applied" in text:
                return {"success": True, "confirmation": "Already applied to this job on Naukri"}
            if any(kw in text for kw in (
                "application submitted", "applied successfully",
                "applied to", "thank you for applying",
                "we have received your application",
                "your application has been sent",
                "successfully applied",
            )):
                return {"success": True, "confirmation": f"Naukri application submitted (answered {answered} questions)"}

            # Check if chatbot is closed or no more interactive elements
            chatbot_open = await page.evaluate("""() => {
                const wrapper = document.querySelector('div.chatbot_DrawerContentWrapper');
                if (!wrapper) return false;
                if (wrapper.offsetParent === null) return false;
                // Any interactive element?
                const ce = wrapper.querySelector('div[contenteditable="true"]');
                const radio = wrapper.querySelector('input[type="radio"]');
                const textInput = wrapper.querySelector('input[type="text"], input[type="number"], textarea');
                const file = wrapper.querySelector('input[type="file"]');
                return !!(ce || radio || textInput || file);
            }""")
            if step > 0 and not chatbot_open:
                # Chatbot closed or no more questions — successful application
                return {"success": True, "confirmation": f"Naukri application submitted (answered {answered} questions)"}

            # --- Read the LAST chatbot question only ---
            question = await page.evaluate("""() => {
                // Get only the last bot message (the current question)
                const botItems = document.querySelectorAll('li.botItem');
                if (botItems.length === 0) return '';
                const lastBot = botItems[botItems.length - 1];
                const span = lastBot.querySelector('span');
                return span ? span.textContent.trim().toLowerCase() : lastBot.textContent.trim().toLowerCase();
            }""")
            logger.info("Naukri chatbot step %d Q: %s", step + 1, question[:60])

            # === 1) RADIO BUTTONS (ssrc__radio) ===
            has_radios = await page.evaluate("""() => {
                return document.querySelectorAll('input[type="radio"]').length > 0 &&
                    Array.from(document.querySelectorAll('input[type="radio"]')).some(r => r.offsetParent !== null);
            }""")

            if has_radios:
                chosen = await page.evaluate(r"""(config) => {
                    const q = config.question;
                    const af = config.af;
                    const expMap = config.expMap;
                    const radios = document.querySelectorAll('input[type="radio"]');
                    const options = [];
                    radios.forEach(r => {
                        if (!r.offsetParent) return;
                        const lbl = document.querySelector('label[for="' + r.id + '"]');
                        options.push({el: r, value: r.value, label: lbl ? lbl.textContent.trim().toLowerCase() : r.value});
                    });
                    if (!options.length) return false;

                    let chosen = null;

                    // Pick "No" for red-flag questions BEFORE any Yes fallback
                    if (/pay cut|salary cut|reduced salary|lower.*ctc|below.*current|\bfresher\b|fresh graduate|criminal|conviction|fraud|terminated|fired|applied (here )?before|previously applied|background.*issue/.test(q)) {
                        chosen = options.find(o => /^no\b|\bno\b/.test(o.label));
                    } else if (/immediate|joiner|notice|join/.test(q)) {
                        // Find closest option to notice period (default 15 days)
                        const noticeStr = af.notice_period || '15 days';
                        const targetDays = parseInt(noticeStr.match(/\d+/)?.[0] || '15');
                        let best = null, bestDiff = 999;
                        options.forEach(o => {
                            const n = parseInt(o.value);
                            if (!isNaN(n) && Math.abs(n - targetDays) < bestDiff) {
                                bestDiff = Math.abs(n - targetDays);
                                best = o;
                            }
                        });
                        chosen = best || options[0];
                    } else if (/gender/.test(q)) {
                        const target = (af.gender || 'male').toLowerCase();
                        chosen = options.find(o => o.label.includes(target)) || options[0];
                    } else if (/relocat|willing to move|shift to|open.*relocat/.test(q)) {
                        chosen = options.find(o => /^yes$|\byes\b/i.test(o.text)) || options[0];
                    } else if (/current.*(location|city)|present.*(location|city)|home.*(location|city)|where.*based|where.*live|residing/.test(q)) {
                        const target = (config.currentCity || '').toLowerCase();
                        if (target) chosen = options.find(o => o.label.includes(target));
                    } else if (/location|city/.test(q)) {
                        const pref = (config.preferredCity || '').toLowerCase();
                        if (pref) chosen = options.find(o => o.label.includes(pref));
                        if (!chosen) {
                            for (const p of (af.preferred_locations || [])) {
                                chosen = options.find(o => o.label.includes(p.toLowerCase()));
                                if (chosen) break;
                            }
                        }
                    } else if (/willing|ready|agree|do you|are you|can you|comfortable|face to face|f2f|interview|onsite|in.person|office/.test(q)) {
                        chosen = options.find(o => /yes|true|0|agree/.test(o.label)) || options[0];
                    } else if (/experience|years/.test(q)) {
                        // Determine target years: specific tech in exp_map → its value;
                        // else if generic total/overall → total_experience;
                        // else if "in <unknown tech>" → 0
                        let target = null;
                        for (const [kw, yrs] of Object.entries(expMap)) {
                            if (q.includes(kw)) { target = parseFloat(yrs); break; }
                        }
                        if (target === null) {
                            if (/total|overall|professional|relevant|your.*experience|how many years.*(have|do)|years of experience\??\s*$/.test(q)) {
                                target = parseFloat(af.total_experience || '4');
                            } else if (/experience\s+(in|with|of|on)\b|years\s+(in|with|of|on)\b/.test(q)) {
                                target = 1;
                            } else {
                                target = parseFloat(af.total_experience || '4');
                            }
                        }

                        // Helper to parse years from option labels/values
                        function parseYears(label) {
                            const l = label.toLowerCase();
                            if (/no experience|fresher|\b0\b|\bzero\b/.test(l)) return 0;
                            let m = l.match(/(\d+)\s*(?:-|to)\s*(\d+)/);
                            if (m) return (parseFloat(m[1]) + parseFloat(m[2])) / 2;
                            m = l.match(/(\d+)\s*\+/);
                            if (m) return parseFloat(m[1]) + 0.5;
                            m = l.match(/(\d+)/);
                            if (m) return parseFloat(m[1]);
                            return -1;
                        }

                        let best = null, bestDiff = 999;
                        // Filter out fresher options to avoid falling back to "No experience"
                        let candidates = options.filter(o => !/no experience|fresher|\b0\b|\bzero\b/i.test(o.label));
                        if (candidates.length === 0) candidates = options;

                        candidates.forEach(o => {
                            let n = parseYears(o.label);
                            if (n === -1) n = parseFloat(o.value);
                            if (!isNaN(n) && n >= 0 && Math.abs(n - target) < bestDiff) {
                                bestDiff = Math.abs(n - target);
                                best = o;
                            }
                        });
                        chosen = best || candidates[0];
                    } else if (/\bbond\b|service agreement|year bond|night shift|us shift|rotational|travel|deputation/.test(q)) {
                        chosen = options.find(o => /^yes$|\byes\b/i.test(o.label)) || options[0];
                    } else if (/offer.*hand|in.*hand.*offer|any.*offer|active offer|other offer|counter.*offer|competing offer/.test(q)) {
                        chosen = options.find(o => /^yes$|\byes\b/i.test(o.label)) || options[0];
                    } else if (/certified|certificat|certification|\bcert\b/.test(q)) {
                        chosen = options.find(o => /^yes$|\byes\b/i.test(o.label)) || options[0];
                    } else if (/mil.*spouse|mil.*partner/.test(q)) {
                        chosen = options.find(o => /not\s*a\s*military|non-military|not\s*a\s*spouse|no\b|not\b/i.test(o.label)) || options[0];
                    }
                    if (!chosen) {
                        chosen = options.find(o => o.label.includes('skip')) || options[0];
                    }
                    if (chosen) { chosen.el.click(); return true; }
                    return false;
                }""", {"question": question, "af": af, "expMap": exp_map, "currentCity": current_city, "preferredCity": preferred_city})
                if chosen:
                    answered += 1

            # === 2) CONTENTEDITABLE DIV (Naukri chatbot text input) ===
            has_contenteditable = await page.evaluate("""() => {
                const ce = document.querySelector('div[contenteditable="true"].textArea, div[contenteditable="true"][data-placeholder]');
                return !!(ce && ce.offsetParent !== null);
            }""")

            if has_contenteditable:
                value = ""
                import re as _re
                # ORDER MATTERS: Check specific data fields BEFORE generic yes/no patterns.
                # --- Negative / red-flag questions: answer No (BEFORE generic Yes) ---
                if _re.search(r"pay cut|salary cut|reduced salary|lower.*ctc|below.*current|less.*than.*current|hike.*acceptable|negotiat", question):
                    value = "Negotiable" if "negotiat" in question else "No"
                elif _re.search(r"\bfresher\b|fresh graduate|just graduat|zero experience|no experience|0 year|0 yr", question):
                    value = "No"
                elif _re.search(r"applied (here )?before|previously applied|reapply|already.*applied", question):
                    value = "No"
                elif _re.search(r"criminal|conviction|fraud|terminated|fired|background.*issue|police.*case", question):
                    value = "No"
                # Graduation / highest qualification — check BEFORE CTC (avoids "percentage" → "age" → 25 bug)
                elif _re.search(r"highest.*(qualification|education|degree)|graduation.*(year|percentage|cgpa|passed out)|passed out.*year|qualification.*passed out|qualification.*year", question):
                    value = af.get("highest_qualification", "B.Tech 2022, CGPA 8.03")
                elif _re.search(r"\b(passed out year|graduation year|year of passing|year of graduation|passing year)\b", question):
                    value = af.get("graduation_year", "2022")
                elif _re.search(r"\bcgpa\b", question):
                    value = af.get("graduation_cgpa", "8.03")
                elif _re.search(r"\b(percentage|aggregate)\b", question):
                    value = af.get("graduation_percentage", "80")
                # Offer-in-hand — check BEFORE CTC (offer Qs often contain "ctc")
                elif _re.search(r"offer.*hand|in.*hand.*offer|any.*offer|live offer|active offer|other offer|counter.*offer|competing offer|how many offer", question):
                    if _re.search(r"how many|number of|count", question):
                        value = "1"
                    elif _re.search(r"mention.*ctc|offered ctc|offer.*amount|amount|how much|value.*offer", question):
                        value = af.get("offer_amount", "14") + " LPA"
                    else:
                        value = af.get("offer_in_hand", "Yes")
                # CTC questions
                elif "current" in question and "ctc" in question:
                    value = af.get("current_ctc", "10")
                elif "expected" in question and "ctc" in question:
                    value = af.get("expected_ctc", "16")
                elif "ctc" in question or "salary" in question or "lpa" in question or "compensation" in question:
                    value = af.get("current_ctc", "10") if "current" in question or "present" in question else af.get("expected_ctc", "16")
                # Experience questions
                elif "experience" in question or "years" in question or ("year" in question and "exp" in question):
                    # 1) Specific tech in our exp_map wins
                    tech_match = None
                    for kw, yrs in exp_map.items():
                        if kw in question:
                            tech_match = yrs
                            break
                    if tech_match is not None:
                        value = tech_match
                    elif _re.search(r"total|overall|professional|relevant|work experience|how many years.*(have|do)|your.*experience|years of experience\??\s*$|years.*industry", question):
                        # Generic "total / overall / your experience" → total
                        value = af.get("total_experience", "3.9")
                    elif _re.search(r"experience\s+(in|with|of|on)\b|years\s+(in|with|of|on)\b", question):
                        # "experience in <X>" but X not in our map — default to 1 yr
                        value = "1"
                    else:
                        value = af.get("total_experience", "3.9")
                elif "notice" in question:
                    notice = af.get("notice_period", "Serving notice period")
                    # Numeric ask → extract digits from notice (e.g. "15 days" → "15"); else "0" since serving
                    if "day" in question or "how many" in question:
                        import re as _r2
                        m = _r2.search(r"\d+", notice)
                        value = m.group(0) if m else "0"
                    else:
                        value = notice
                # Last Working Day (LWD) questions
                elif _re.search(r"last working day|lwd|last day|when.*leave|when.*available|when.*join", question):
                    value = af.get("last_working_day", "Currently Working")
                # Primary cloud / preferred cloud questions
                elif _re.search(r"primary cloud|preferred cloud|main cloud|cloud platform|which cloud", question):
                    value = af.get("primary_cloud", "GCP")
                # Contract / contract-based hiring questions
                elif _re.search(r"contract|contractual|c2h|contract.based|contract to hire|contract role|short term", question):
                    value = af.get("contract_based", "Yes")
                elif "name" in question and "company" not in question:
                    value = cfg.name
                elif "email" in question:
                    value = cfg.email
                elif "phone" in question or "mobile" in question:
                    value = cfg.phone
                # Relocation: answer Yes (check BEFORE generic location to avoid being typed as a city)
                elif _re.search(r"relocat|willing to move|shift to|open.*relocat", question):
                    value = "Yes"
                # Combined "current and preferred location" — answer both
                elif _re.search(r"current\s+and\s+preferred|current\s*&\s*preferred|preferred\s+and\s+current", question):
                    value = f"{current_city} and {preferred_city}" if preferred_city and preferred_city != current_city else current_city
                # Current location questions → current city (Jaipur)
                elif _re.search(r"current.*(location|city)|present.*(location|city)|home.*(location|city)|where.*based|where.*live|where.*you.*from|residing", question):
                    value = current_city
                # Preferred location questions → JD city (falls back to current)
                elif _re.search(r"preferred.*(location|city)|prefer.*(location|city)|location.*prefer|interested.*location", question):
                    value = preferred_city
                # Generic location/city — default to current (most common ask)
                elif "location" in question or "city" in question:
                    value = current_city
                elif _re.search(r"\bage\b", question):
                    value = "25"
                # Bond / service agreement — Yes (typical for IT firms)
                elif _re.search(r"\bbond\b|service agreement|service.based bond|year bond|company bond", question):
                    value = "Yes"
                # Shifts / travel — Yes
                elif _re.search(r"night shift|us shift|uk shift|rotational shift|24.?7|round the clock|travel|deputation", question):
                    value = "Yes"
                # Yes/No text questions (AFTER specific data fields & negatives) — narrower patterns
                elif _re.search(r"comfortable|willing|ready|agree to|face to face|f2f|onsite|in.person|office|can you join|ok with|okay with", question):
                    value = "Yes"
                elif _re.search(r"certified|certification|certificate|certificates|\bcert\b", question):
                    value = af.get("certifications", "AWS solutions architect associate and AWS cloudops enginerr associate and AWS AI Practitioner")
                elif _re.search(r"mil.*spouse|mil.*partner", question):
                    value = "No"
                elif _re.search(r"do you|are you|have you|can you|will you|would you", question):
                    value = "Yes"


                if value:
                    # Click the contenteditable, clear it, type via keyboard
                    ce_el = await page.query_selector('div[contenteditable="true"].textArea, div[contenteditable="true"][data-placeholder]')
                    if ce_el:
                        await ce_el.click()
                        await page.wait_for_timeout(300)
                        # Select all + delete to clear any old text
                        await page.keyboard.press("Control+a")
                        await page.keyboard.press("Meta+a")
                        await page.keyboard.press("Delete")
                        await page.wait_for_timeout(200)
                        # Type the value character by character
                        await page.keyboard.type(str(value), delay=50)
                        await page.wait_for_timeout(500)
                        answered += 1
                        logger.info("Chatbot: typed '%s' for Q: %s", value, question[:40])

            # === 3) STANDARD TEXT INPUTS (fallback for non-chatbot forms) ===
            elif not has_radios:
                await _autofill_fields(page, cfg, job_location)

            # === 4) RESUME UPLOAD — only when question asks for it ===
            if cfg.resume_exists and any(w in question for w in ("resume", "cv", "upload", "attach")):
                uploaded = await _upload_resume(page, cfg.resume_path)
                if uploaded:
                    answered += 1

            await page.wait_for_timeout(1000)

            # === 5) CLICK SAVE ===
            # Naukri chatbot Save is: <div class="send"><div class="sendMsg">Save</div></div>
            # The parent div has "disabled" class until input has text.
            # First remove disabled class, then click.
            save_clicked = await page.evaluate("""() => {
                // Remove disabled from send container
                const sendContainer = document.querySelector('div.send, div[class*="sendMsgbtn_container"] div.send');
                if (sendContainer) {
                    sendContainer.classList.remove('disabled');
                }
                // Click the sendMsg div
                const sendMsg = document.querySelector('div.sendMsg');
                if (sendMsg) {
                    sendMsg.click();
                    return 'sendMsg';
                }
                // Also try clicking the container itself
                if (sendContainer) {
                    sendContainer.click();
                    return 'sendContainer';
                }
                // Fallback: any element with text "Save"
                const all = document.querySelectorAll('div, button, a, span');
                for (const el of all) {
                    if (el.offsetParent === null) continue;
                    const t = el.textContent.trim();
                    if (t === 'Save' || t === 'Submit' || t === 'Next') {
                        el.click();
                        return t;
                    }
                }
                return '';
            }""")
            if save_clicked:
                logger.info("Naukri: clicked %s", save_clicked)

        # Final check
        text = (await page.inner_text("body")).lower()
        if "already applied" in text:
            return {"success": True, "confirmation": "Already applied to this job on Naukri"}
        if "application submitted" in text or "applied successfully" in text:
            return {"success": True, "confirmation": f"Naukri applied (answered {answered} questions)"}

        return {"success": True, "confirmation": f"Naukri apply completed (answered {answered} questions)"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}








PLATFORM_APPLYERS = {
    "naukri": _apply_naukri,
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
        browser = await pw.chromium.launch(channel="chrome", headless=False)
        context = await browser.new_context(
            user_agent=get_user_agent(),
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

        async def _process_job(context, job, idx, total):
            url = job["apply_url"]
            plat = job["platform"]
            title = job.get("title", "Unknown")
            company = job.get("company", "Unknown")
            logger.info("[%d/%d] Applying: %s @ %s", idx + 1, total, title, company)

            result = await _apply_in_tab(
                context, url, plat, cfg, job.get("cover_note", ""),
            )
            status = "applied" if result.get("success") else "failed"
            try:
                record_application(
                    job_title=title, company=company, platform=plat,
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

            for i, job in enumerate(to_apply):
                await _process_job(context, job, i, len(to_apply))
                if i < len(to_apply) - 1:
                    delay = random.uniform(5, 15)
                    await asyncio.sleep(delay)

            try:
                await save_cookies_from_context(context, "naukri")
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
