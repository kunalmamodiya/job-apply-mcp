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

from pathlib import Path

from config import APP_DIR, AppConfig, get_user_agent, load_config
from tools.session import load_cookies, save_cookies_from_context
from tools.tracker import is_already_applied, record_application

BROWSER_PROFILES_DIR = APP_DIR / "browser-profiles"

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Smart form auto-filler
# ---------------------------------------------------------------------------

async def _autofill_fields(page: Page, cfg: AppConfig) -> int:
    """
    Scan all visible input/select/textarea fields on the page and fill them
    using the autofill config via JavaScript for reliability.
    Returns the number of fields filled.
    """
    af = cfg.autofill
    if not af:
        return 0

    # Build the full answer map to pass into JS
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
        "location": af.get("preferred_locations", [""])[0] if af.get("preferred_locations") else cfg.location,
        "city": af.get("preferred_locations", [""])[0] if af.get("preferred_locations") else cfg.location,
        "preferred location": af.get("preferred_locations", [""])[0] if af.get("preferred_locations") else cfg.location,
        "current location": cfg.location,
    }
    # Add experience keywords
    for k, v in af.get("experience", {}).items():
        answers[k.lower()] = v

    pref_locs = af.get("preferred_locations", [])

    # ---- Use JavaScript to find and fill all visible fields ----
    filled = await page.evaluate('''(config) => {
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
            } else if (ctx.includes("location") || ctx.includes("city") || ctx.includes("preferred")) {
                for (const pref of prefLocs) {
                    chosen = options.find(o => o.text.includes(pref.toLowerCase()));
                    if (chosen) break;
                }
            } else if (ctx.includes("notice")) {
                const target = (answers.notice_period || "immediate").toLowerCase();
                chosen = options.find(o => o.text.includes(target));
            } else if (ctx.includes("experience") || ctx.includes("exp")) {
                chosen = options.find(o => /[34]/.test(o.text));
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
            let target = null;
            if (ctx.includes("gender")) target = (answers.gender || "male").toLowerCase();
            else if (ctx.includes("location") || ctx.includes("remote")) target = prefLocs[0]?.toLowerCase();

            if (target) {
                const match = group.find(r => r.value.includes(target) || r.label.includes(target));
                if (match) {
                    match.el.checked = true;
                    match.el.dispatchEvent(new Event("change", {bubbles: true}));
                    filled++;
                }
            }
        }

        return filled;
    }''', {"answers": answers, "prefLocs": pref_locs})

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

async def _apply_linkedin(page: Page, cfg: AppConfig, cover_note: str) -> dict[str, Any]:
    """Apply via LinkedIn Easy Apply with autofill and multi-step handling."""
    try:
        # Find Easy Apply button — use aria-label which is most reliable
        easy_apply_btn = await page.evaluate('''() => {
            const btns = document.querySelectorAll('button');
            for (const btn of btns) {
                const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                const text = btn.textContent.trim().toLowerCase();
                const isApply = aria.startsWith('easy apply to') ||
                    (text === 'easy apply' && btn.className.includes('jobs-apply'));
                if (isApply && btn.offsetParent !== null) {
                    return true;
                }
            }
            return false;
        }''')

        if not easy_apply_btn:
            return {"success": False, "error": "No Easy Apply button — external apply, skipped"}

        # Click it via JS
        await page.evaluate('''() => {
            const btns = document.querySelectorAll('button');
            for (const btn of btns) {
                const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                if (aria.startsWith('easy apply to') && btn.offsetParent !== null) {
                    btn.click();
                    return;
                }
            }
        }''')

        if not easy_apply_btn:
            return {"success": False, "error": "No Easy Apply button — external apply, skipped"}

        await easy_apply_btn.click()
        await page.wait_for_timeout(2500)

        # Multi-step loop: autofill, upload resume, click Next/Submit
        filled_total = 0
        for step in range(8):
            # Autofill all visible fields
            filled_total += await _autofill_fields(page, cfg)

            # Upload resume if prompted
            if cfg.resume_exists:
                await _upload_resume(page, cfg.resume_path)

            # Check for success / dismiss
            text = (await page.inner_text("body")).lower()
            if "application submitted" in text or "your application was sent" in text:
                dismiss = await page.query_selector(
                    "button[aria-label='Dismiss'], button:has-text('Done')"
                )
                if dismiss:
                    try:
                        await dismiss.click()
                    except Exception:
                        pass
                return {"success": True, "confirmation": "LinkedIn Easy Apply submitted"}

            # Find Submit or Next button
            submit_btn = await page.query_selector(
                "button[aria-label='Submit application'], "
                "button[aria-label='Review your application'], "
                "button:has-text('Submit application')"
            )
            next_btn = await page.query_selector(
                "button[aria-label='Continue to next step'], "
                "button[aria-label='Next'], "
                "button:has-text('Next'), "
                "button:has-text('Review')"
            )

            if submit_btn:
                await submit_btn.click()
                await page.wait_for_timeout(2500)
                text2 = (await page.inner_text("body")).lower()
                if "application submitted" in text2 or "your application was sent" in text2:
                    return {"success": True, "confirmation": "LinkedIn Easy Apply submitted"}
            elif next_btn:
                await next_btn.click()
                await page.wait_for_timeout(1500)
            else:
                break

        return {"success": True, "confirmation": f"LinkedIn apply completed (autofilled {filled_total} fields)"}
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
                chosen = await page.evaluate("""(config) => {
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

                    if (/immediate|joiner|notice|join/.test(q)) {
                        chosen = options.find(o => o.value === '0') || options[0];
                    } else if (/gender/.test(q)) {
                        const target = (af.gender || 'male').toLowerCase();
                        chosen = options.find(o => o.label.includes(target)) || options[0];
                    } else if (/location|relocat|city/.test(q)) {
                        for (const pref of (af.preferred_locations || [])) {
                            chosen = options.find(o => o.label.includes(pref.toLowerCase()));
                            if (chosen) break;
                        }
                    } else if (/willing|ready|agree|do you|are you|can you|comfortable|face to face|f2f|interview|onsite|in.person|office/.test(q)) {
                        chosen = options.find(o => /yes|true|0|agree/.test(o.label)) || options[0];
                    } else if (/experience|years/.test(q)) {
                        const target = parseFloat(af.total_experience || '4');
                        let best = null, bestDiff = 999;
                        options.forEach(o => {
                            const n = parseFloat(o.value);
                            if (!isNaN(n) && Math.abs(n - target) < bestDiff) { bestDiff = Math.abs(n - target); best = o; }
                        });
                        chosen = best;
                    }
                    if (!chosen) {
                        chosen = options.find(o => o.label.includes('skip')) || options[0];
                    }
                    if (chosen) { chosen.el.click(); return true; }
                    return false;
                }""", {"question": question, "af": af, "expMap": exp_map})
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
                # CTC questions
                if "current" in question and "ctc" in question:
                    value = af.get("current_ctc", "9")
                elif "expected" in question and "ctc" in question:
                    value = af.get("expected_ctc", "16")
                elif "ctc" in question or "salary" in question or "lpa" in question or "compensation" in question:
                    value = af.get("current_ctc", "9") if "current" in question or "present" in question else af.get("expected_ctc", "16")
                # Experience questions (including "how many years of experience do you have in X")
                elif "experience" in question or "years" in question or ("year" in question and "exp" in question):
                    value = af.get("total_experience", "3.9")
                    # Match specific tech keywords first
                    for kw, yrs in exp_map.items():
                        if kw in question:
                            value = yrs
                            break
                elif "notice" in question:
                    value = "0"
                elif "name" in question and "company" not in question:
                    value = cfg.name
                elif "email" in question:
                    value = cfg.email
                elif "phone" in question or "mobile" in question:
                    value = cfg.phone
                elif "location" in question or "city" in question:
                    pref = af.get("preferred_locations", [])
                    value = pref[0] if pref else cfg.location
                elif "age" in question:
                    value = "25"
                # Yes/No text questions (AFTER specific data fields) — narrower patterns
                elif _re.search(r"comfortable|willing|ready|agree to|face to face|f2f|onsite|in.person|office|relocat|can you join|ok with|okay with", question):
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
                await _autofill_fields(page, cfg)

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
        # LinkedIn needs persistent browser profile for auth
        if platform == "linkedin":
            profile_dir = str(BROWSER_PROFILES_DIR / "linkedin")
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            context = await pw.firefox.launch_persistent_context(
                profile_dir, headless=False,
                viewport={"width": 1280, "height": 800},
            )
            page = context.pages[0] if context.pages else await context.new_page()
            is_persistent = True

            # LinkedIn Easy Apply only works from the search page with job selected.
            # Extract job ID and navigate to search page with currentJobId.
            import re as _re
            job_id_match = _re.search(r'/jobs/view/(\d+)', job_url) or _re.search(r'currentJobId=(\d+)', job_url)
            if job_id_match:
                job_id = job_id_match.group(1)
                nav_url = f"https://www.linkedin.com/jobs/search/?currentJobId={job_id}&f_AL=true"
            else:
                nav_url = job_url
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(4000)
        else:
            browser = await pw.firefox.launch(headless=False)
            context = await browser.new_context(
                user_agent=get_user_agent(),
                viewport={"width": 1280, "height": 800},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                ignore_https_errors=True,
            )
            await load_cookies(context, platform)
            page = await context.new_page()
            is_persistent = False

            await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3000)

        if await _detect_captcha(page):
            await context.close() if is_persistent else await browser.close()
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

        # Save cookies after apply (non-LinkedIn only)
        if not is_persistent:
            try:
                await save_cookies_from_context(context, platform)
            except Exception:
                pass

        if is_persistent:
            await context.close()
        else:
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
        # LinkedIn: navigate to search page with currentJobId for Easy Apply
        if platform == "linkedin":
            import re as _re
            match = _re.search(r'/jobs/view/(\d+)', job_url) or _re.search(r'currentJobId=(\d+)', job_url)
            if match:
                nav_url = f"https://www.linkedin.com/jobs/search/?currentJobId={match.group(1)}&f_AL=true"
            else:
                nav_url = job_url
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=25_000)
            await page.wait_for_timeout(4000)
        else:
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

        # Split jobs: LinkedIn (persistent profile) vs others (shared context)
        linkedin_jobs = [j for j in to_apply if j.get("platform") == "linkedin"]
        other_jobs = [j for j in to_apply if j.get("platform") != "linkedin"]

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
            # --- LinkedIn jobs: persistent browser profile ---
            if linkedin_jobs:
                profile_dir = str(BROWSER_PROFILES_DIR / "linkedin")
                Path(profile_dir).mkdir(parents=True, exist_ok=True)
                li_ctx = await pw.firefox.launch_persistent_context(
                    profile_dir, headless=False,
                    viewport={"width": 1280, "height": 800},
                )
                for i, job in enumerate(linkedin_jobs):
                    await _process_job(li_ctx, job, i, len(linkedin_jobs))
                    if i < len(linkedin_jobs) - 1:
                        await asyncio.sleep(random.uniform(5, 15))
                await li_ctx.close()

            # --- Other platform jobs: shared context ---
            if other_jobs:
                browser = await pw.firefox.launch(headless=False)
                context = await browser.new_context(
                    user_agent=get_user_agent(),
                    viewport={"width": 1280, "height": 800},
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                    ignore_https_errors=True,
                )
                platforms_loaded = set()
                for job in other_jobs:
                    p = job.get("platform", "")
                    if p and p not in platforms_loaded:
                        await load_cookies(context, p)
                        platforms_loaded.add(p)

                for i, job in enumerate(other_jobs):
                    await _process_job(context, job, i, len(other_jobs))
                    if i < len(other_jobs) - 1:
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
