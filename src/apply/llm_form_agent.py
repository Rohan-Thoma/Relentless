from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any

import httpx

from src.config import load_config
from src.httpx_utils import post_with_retry
from src.model_catalog import resolve_chat_endpoint
from src.settings_store import (
    ProfileNotConfigured,
    get_master_profile,
    load_example_profile,
)

logger = logging.getLogger(__name__)


def resolve_candidate_profile(profile: dict | None = None) -> dict:
    """Ensures a valid candidate profile is available without hardcoded PII."""
    if profile and isinstance(profile, dict) and (profile.get("name") or profile.get("application_defaults")):
        if not profile.get("application_defaults"):
            try:
                ex = load_example_profile()
                profile["application_defaults"] = ex.get("application_defaults", {})
            except Exception:
                pass
        return profile

    try:
        loaded = get_master_profile()
        if loaded:
            return loaded
    except ProfileNotConfigured:
        pass

    try:
        ex = load_example_profile()
        if ex:
            return ex
    except Exception:
        pass

    raise ProfileNotConfigured(
        "Candidate profile is required for form filling. Configure config/master_profile.json "
        "or set ALLOW_EXAMPLE_PROFILE=1."
    )


async def dismiss_popups_and_open_form(page):
    """Stage 1:
    - Auto-dismisses visible cookie banners & GDPR overlays (OneTrust, Cookiebot, generic).
    - Clicks 'Apply for this job' / 'Apply Manually' if no applicant form inputs are visible yet.
    - Resolves embedded ATS iframes (Greenhouse, Ashby, Lever, SmartRecruiters).
    """
    cookie_selectors = [
        "#onetrust-accept-btn-handler",
        "#accept-recommended-btn-handler",
        "button#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        "button:has-text('Accept All')",
        "button:has-text('Accept Cookies')",
        "button:has-text('Allow All')",
        "button:has-text('I Agree')",
        "button:has-text('Got it')",
        "[aria-label*='accept cookie' i]",
        "[aria-label*='dismiss' i]",
    ]
    for sel in cookie_selectors:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await page.wait_for_timeout(400)
                break
        except Exception:
            pass

    # Check whether REAL non-navigation form inputs already exist
    try:
        real_input_count = await page.evaluate(
            """() => {
            const isVisible = (el) => {
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const els = Array.from(document.querySelectorAll('input:not([type=hidden]):not([type=search]), textarea, select'))
                .filter(el => isVisible(el) && !el.closest('header, nav, footer, [role="search"]'));
            const files = Array.from(document.querySelectorAll('input[type="file"]'))
                .filter(el => !el.closest('header, nav, footer'));
            return els.length + files.length;
        }"""
        )
    except Exception:
        real_input_count = 0

    if real_input_count == 0:
        apply_selectors = [
            "button:has-text('Apply for this job')",
            "a:has-text('Apply for this job')",
            "button:has-text('Apply Manually')",
            "a:has-text('Apply Manually')",
            "button:has-text('Apply Now')",
            "a:has-text('Apply Now')",
            "main button:has-text('Apply')",
            "main a[role='button']:has-text('Apply')",
        ]
        for sel in apply_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(1500)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=4000)
                    except Exception:
                        pass
                    break
            except Exception:
                pass

    # Detect embedded ATS application iframe
    target_frame = page
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            frame_inputs = await frame.query_selector_all("input:not([type=hidden]):not([type=search]), textarea, select")
            if len(frame_inputs) >= 2:
                target_frame = frame
                break
        except Exception:
            pass

    return target_frame


async def extract_form_schema(frame_or_page) -> list[dict]:
    """Stage 2:
    Indexes visible applicant form inputs, textareas, selects, and comboboxes —
    STRICTLY EXCLUDING <header>, <nav>, <footer>, and search bars so Playwright can never
    type into site search boxes!
    """
    schema = await frame_or_page.evaluate(
        """() => {
        const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        };

        const isNavOrSearch = (el) => {
            if (el.closest('header, nav, footer, [role="navigation"], [role="search"], [role="banner"]')) return true;
            const type = (el.getAttribute('type') || '').toLowerCase();
            if (type === 'search') return true;
            const meta = ((el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
            if (meta.includes('search') || meta.includes('keyword') || meta.includes('filter jobs')) return true;
            return false;
        };

        const resolveLabel = (el) => {
            const parts = [];
            let base = '';
            if (el.id) {
                try {
                    const forLbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    if (forLbl) base = forLbl.innerText.trim();
                } catch (e) {}
            }
            if (!base && el.labels && el.labels.length > 0) {
                base = el.labels[0].innerText.trim();
            }
            if (!base && el.getAttribute('aria-label')) {
                base = el.getAttribute('aria-label').trim();
            }
            if (!base && el.getAttribute('aria-labelledby')) {
                const ref = document.getElementById(el.getAttribute('aria-labelledby'));
                if (ref) base = ref.innerText.trim();
            }
            if (!base) {
                const parent = el.closest('label, .form-group, .field, [class*="field"], [class*="question"], tr');
                if (parent) {
                    const lbl = parent.querySelector('label, [class*="label"], legend');
                    if (lbl && lbl !== el) {
                        base = lbl.innerText.trim();
                    }
                }
            }
            const fieldset = el.closest('fieldset');
            const legend = fieldset ? fieldset.querySelector('legend') : null;
            const legendText = legend ? legend.innerText.trim() : '';
            if (legendText && (!base || !base.toLowerCase().includes(legendText.toLowerCase().slice(0, 25)))) {
                parts.push(legendText);
            }
            if (base) parts.push(base);
            return parts.join(' — ').slice(0, 240);
        };

        const elements = Array.from(document.querySelectorAll(
            'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([type=reset]):not([type=search]), textarea, select, [role="combobox"]'
        )).filter(el => isVisible(el) && !isNavOrSearch(el));

        return elements.map((el, index) => {
            const fieldId = `field_${index}`;
            el.setAttribute('data-relentless-id', fieldId);

            const tag = el.tagName.toLowerCase();
            const roleAttr = (el.getAttribute('role') || '').toLowerCase();
            const isCombo = roleAttr === 'combobox'
                || Boolean(el.getAttribute('aria-autocomplete'))
                || (el.className || '').toString().includes('select__input');
            const rawType = (el.getAttribute('type') || (tag === 'textarea' ? 'textarea' : tag)).toLowerCase();
            const type = isCombo ? 'combobox' : rawType;
            const name = el.getAttribute('name') || '';
            const id = el.getAttribute('id') || '';
            const placeholder = el.getAttribute('placeholder') || '';
            const required = el.required || el.getAttribute('aria-required') === 'true' || el.classList.contains('required');
            const label = resolveLabel(el);

            let options = [];
            if (tag === 'select') {
                options = Array.from(el.options).map(opt => ({
                    value: opt.value,
                    text: opt.innerText.trim()
                }));
            }

            return {
                field_id: fieldId,
                tag: tag,
                type: type,
                name: name,
                group_name: (type === 'checkbox' || type === 'radio') ? name : '',
                id: id,
                placeholder: placeholder,
                required: required,
                label: label,
                options: options,
                current_value: el.value || ''
            };
        });
    }"""
    )
    return schema or []


def _generate_motive_statement(job: dict, profile: dict) -> str:
    company = job.get("company") or "the company"
    title = job.get("title") or "this role"
    jd_text = (job.get("jd_text") or "").strip()

    mission_snippet = ""
    lines = [ln.strip() for ln in jd_text.splitlines() if ln.strip()]
    for ln in lines[:10]:
        if re.search(r"\b(mission|building|empower|innovat|revolution|leading|product)\b", ln, re.IGNORECASE):
            mission_snippet = ln[:120].strip(" -:.")
            break

    summary = (
        profile.get("experience_summary")
        or profile.get("application_defaults", {}).get("current_title")
        or "engineering experience"
    )

    if mission_snippet:
        return (
            f"I am excited to apply for the {title} role at {company}. "
            f"Given your focus on {mission_snippet}, my background in {summary} "
            f"positions me to make an immediate, high-ownership impact."
        )

    return (
        f"I am eager to contribute to {company} as {title}. "
        f"My background in {summary} directly aligns with the technical goals and engineering challenges "
        f"outlined in the role, and I look forward to delivering measurable results for the team."
    )


def _heuristic_plan_fill(fields_schema: list[dict], job: dict, profile: dict) -> dict:
    app_defaults = profile.get("application_defaults") or {}
    name_str = profile.get("name") or app_defaults.get("full_name") or ""
    parts = name_str.split()
    first_name = app_defaults.get("first_name") or (parts[0] if parts else "")
    last_name = app_defaults.get("last_name") or (parts[-1] if len(parts) > 1 else "")

    plan = {}
    chosen_groups: set[str] = set()
    for item in fields_schema:
        fid = item["field_id"]
        key = " ".join([
            item.get("name", ""),
            item.get("id", ""),
            item.get("placeholder", ""),
            item.get("label", ""),
        ]).lower()

        tag = item.get("tag")
        itype = item.get("type", "")
        options = item.get("options") or []
        group_name = item.get("group_name") or ""

        val = None
        if itype in ("checkbox", "radio"):
            # Required legal / privacy / AI acknowledgement / GDPR consent checkboxes
            if any(tok in key for tok in ("acknowledge", "consent", "privacy", "confirm i have read", "agree")):
                val = "true"
            elif "how did you hear" in key or "hear about" in key:
                if group_name not in chosen_groups and any(tok in key for tok in ("linkedin", "careers website", "job board")):
                    val = "true"
                    if group_name:
                        chosen_groups.add(group_name)
        elif itype == "email" or "email" in key:
            val = profile.get("email") or app_defaults.get("email")
        elif itype == "tel" or ("phone" in key and "country" not in key) or "mobile" in key:
            val = profile.get("phone") or app_defaults.get("phone")
        elif ("preferred" in key or "first" in key) and "name" in key:
            val = first_name
        elif "last" in key and "name" in key:
            val = last_name
        elif ("full" in key and "name" in key) or ("name" in key and "company" not in key and "user" not in key):
            val = name_str
        elif "linkedin" in key:
            li = profile.get("linkedin") or app_defaults.get("linkedin") or ""
            val = f"https://{li}" if li and not li.startswith("http") else li
        elif "github" in key:
            gh = profile.get("github") or app_defaults.get("github") or ""
            val = f"https://{gh}" if gh and not gh.startswith("http") else gh
        elif "website" in key or "portfolio" in key:
            val = profile.get("github") or profile.get("linkedin")
        elif "notice" in key:
            val = app_defaults.get("notice_period") or app_defaults.get("notice_period_days")
        elif "current" in key and ("ctc" in key or "salary" in key or "compensation" in key):
            val = app_defaults.get("current_ctc_lpa")
        elif ("expected" in key or "desired" in key) and ("ctc" in key or "salary" in key or "compensation" in key):
            val = app_defaults.get("expected_ctc_lpa")
        elif "source of your right to work" in key or "right to work" in key:
            val = "Citizen"
        elif "auth" in key or "legally" in key or "eligib" in key:
            val = app_defaults.get("authorized_to_work_in_india") or "Yes"
        elif "sponsorship" in key or "visa" in key:
            val = app_defaults.get("requires_visa_sponsorship") or "No"
        elif "relocat" in key:
            val = app_defaults.get("willing_to_relocate") or "Yes"
        elif "country" in key:
            val = app_defaults.get("country") or "India"
        elif "city" in key or "location" in key:
            val = app_defaults.get("current_city") or profile.get("location")
        elif "company" in key or "organization" in key or "employer" in key:
            exp = profile.get("experience") or []
            val = app_defaults.get("current_company") or (exp[0].get("company") if exp else "")
        elif "title" in key or "designation" in key or "role" in key:
            exp = profile.get("experience") or []
            val = app_defaults.get("current_title") or (exp[0].get("role") if exp else "")
        elif "experience" in key and ("total" in key or "year" in key):
            val = app_defaults.get("total_experience_years") or app_defaults.get("relevant_experience_years")
        elif "degree" in key or "education" in key or "university" in key or "college" in key:
            edu = profile.get("education") or []
            val = app_defaults.get("highest_education") or (edu[0].get("degree") if edu else "")
        elif "gender" in key:
            val = app_defaults.get("gender") or "Decline to self-identify"
        elif "veteran" in key:
            val = app_defaults.get("veteran_status") or "I am not a protected veteran"
        elif "disability" in key:
            val = app_defaults.get("disability_status") or "No, I do not have a disability"
        elif tag == "textarea":
            val = _generate_motive_statement(job, profile)

        if val is not None:
            if tag == "select" and options:
                matched_opt = None
                for opt in options:
                    if opt["value"].lower() == str(val).lower() or opt["text"].lower() == str(val).lower():
                        matched_opt = opt["value"] or opt["text"]
                        break
                    if str(val).lower() in opt["text"].lower():
                        matched_opt = opt["value"] or opt["text"]
                        break
                plan[fid] = matched_opt or (options[1]["value"] if len(options) > 1 else str(val))
            else:
                plan[fid] = str(val)

    return plan


async def plan_form_fill_with_llm(fields_schema: list[dict], job: dict, profile: dict) -> dict:
    """Uses configured cfg.models.browser_agent (default openai/gpt-4o) to map form fields + essays."""
    resolved_prof = resolve_candidate_profile(profile)
    heuristic_plan = _heuristic_plan_fill(fields_schema, job, resolved_prof)

    if not fields_schema or os.getenv("PYTEST_CURRENT_TEST"):
        return heuristic_plan

    try:
        cfg = load_config()
        llm_url, api_key, model_id = resolve_chat_endpoint(
            cfg.models.browser_agent.provider, cfg.models.browser_agent.model
        )
    except Exception:
        return heuristic_plan

    if not llm_url or not api_key:
        return heuristic_plan

    app_defaults = resolved_prof.get("application_defaults") or {}
    clean_profile = {
        "name": resolved_prof.get("name"),
        "email": resolved_prof.get("email"),
        "phone": resolved_prof.get("phone"),
        "location": resolved_prof.get("location"),
        "linkedin": resolved_prof.get("linkedin"),
        "github": resolved_prof.get("github"),
        "experience_summary": resolved_prof.get("experience_summary"),
        "experience": resolved_prof.get("experience", [])[:3],
        "core_skills": resolved_prof.get("core_skills", [])[:20],
        "application_defaults": app_defaults,
    }

    prompt = {
        "job": {
            "company": job.get("company"),
            "title": job.get("title"),
            "jd_text_context": (job.get("jd_text") or "")[:4000],
            "apply_url": job.get("apply_url"),
        },
        "candidate_profile": clean_profile,
        "form_fields": fields_schema,
    }

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await post_with_retry(
                client,
                llm_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model_id,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are an intelligent Job Application Autofill Agent. "
                                "Given verified candidate profile data, JD context, and extracted application form fields, "
                                "return a JSON mapping from field_id to the exact string value to fill or select option value. "
                                "Rules:\n"
                                "1. NEVER fabricate credentials.\n"
                                "2. For type='combobox' fields (React Select / Greenhouse / Ashby dropdowns & city autocompletes), return a clean concise search term (e.g., 'India', 'Hyderabad', 'Yes', 'Citizen', 'Decline to self-identify').\n"
                                "3. For checkbox groups sharing the same group_name (like 'How did you hear about us?'), set ONLY ONE best option to 'true' and omit the rest.\n"
                                "4. For required privacy/data/AI policy acknowledgement checkboxes, set 'true'.\n"
                                "5. For motivation/essay textareas, write a grounded 2-3 sentence answer referencing the company and candidate's real projects.\n"
                                "Return ONLY valid JSON: {\"field_0\": \"value\", ...}"
                            ),
                        },
                        {"role": "user", "content": json.dumps(prompt)},
                    ],
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                },
            )
            if resp.status_code == 200:
                parsed = json.loads(resp.json()["choices"][0]["message"]["content"])
                if isinstance(parsed, dict):
                    merged = dict(heuristic_plan)
                    merged.update(parsed)
                    return merged
    except Exception as e:
        logger.warning("LLM form planning failed, falling back to heuristics: %s", e)

    return heuristic_plan


async def plan_vision_navigation_step(
    screenshot_path: str,
    buttons_summary: list[dict[str, Any]],
    job: dict[str, Any],
) -> dict[str, Any]:
    """Vision + DOM planner for multi-step navigation (cookie modals, hidden Apply buttons, multi-page wizards).

    Explicitly refuses automated CAPTCHA bypassing — returns {"action": "HUMAN_GATE", "reason": ...}
    so ApplyAgentGraph hands control cleanly to the user!
    """
    if os.getenv("PYTEST_CURRENT_TEST") or not screenshot_path or not os.path.exists(screenshot_path):
        return {"action": "STOP", "reason": "vision skipped in offline/test mode"}

    try:
        cfg = load_config()
        llm_url, api_key, model_id = resolve_chat_endpoint(
            cfg.models.browser_agent.provider, cfg.models.browser_agent.model
        )
    except Exception:
        return {"action": "STOP", "reason": "no model configured"}

    if not llm_url or not api_key:
        return {"action": "STOP", "reason": "no API key"}

    with open(screenshot_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("ascii")

    prompt_text = (
        f"You are navigating a job application webpage for {job.get('title')} at {job.get('company')}.\n"
        f"Visible clickable non-nav elements: {json.dumps(buttons_summary[:25])}\n\n"
        "Decide the SINGLE next action:\n"
        "- {\"action\": \"CLICK\", \"selector\": \"...\", \"reason\": \"dismiss cookie banner or open application form\"}\n"
        "- {\"action\": \"HUMAN_GATE\", \"reason\": \"CAPTCHA, bot verification, or login/create-account wall detected\"}\n"
        "- {\"action\": \"READY\", \"reason\": \"application form inputs are visible and ready to fill\"}\n"
        "- {\"action\": \"STOP\", \"reason\": \"cannot proceed automatically\"}\n"
        "NEVER attempt to click or solve a CAPTCHA widget — always return HUMAN_GATE for CAPTCHAs or logins."
    )

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await post_with_retry(
                client,
                llm_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model_id,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt_text},
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                            ],
                        }
                    ],
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                },
            )
            if resp.status_code == 200:
                parsed = json.loads(resp.json()["choices"][0]["message"]["content"])
                if isinstance(parsed, dict) and parsed.get("action"):
                    return parsed
    except Exception as e:
        logger.debug("Vision navigation step skipped: %s", e)
    return {"action": "STOP", "reason": "vision unavailable"}


async def _fill_combobox(page, target, el, raw_val: str) -> str | None:
    """Fills modern React-Select / Greenhouse / Ashby comboboxes and async geo-autocompletes.

    Waits for [role='option'] / .select__option menu items to render and clicks the best match
    so the chosen value persists cleanly after blur.
    """
    val_str = str(raw_val or "").strip()
    if not val_str:
        return None

    # Build candidate search queries (full value first, then primary city/token fallback)
    queries = [val_str]
    primary = val_str.split(",")[0].strip()
    if primary and primary.lower() != val_str.lower():
        queries.append(primary)

    option_selector = '[role="option"], .select__option, [id*="react-select"][id*="-option-"]'

    for query in queries:
        try:
            await el.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            await el.click(force=True, timeout=3000)
        except Exception:
            try:
                await el.focus()
            except Exception:
                return None

        # Clear any prior text and type query to trigger local filter or async geo API
        try:
            await el.fill("")
            await el.type(query, delay=25)
        except Exception:
            try:
                await el.fill(query)
            except Exception:
                return None

        # Wait up to 2.0s for React-Select options (async city lookup takes ~400-1200ms)
        visible_opts = []
        for _ in range(10):
            await page.wait_for_timeout(200)
            try:
                opts = await target.query_selector_all(option_selector)
                visible_opts = []
                for opt in opts:
                    if await opt.is_visible():
                        txt = (await opt.inner_text() or "").strip()
                        if txt and "no options" not in txt.lower() and "loading" not in txt.lower():
                            visible_opts.append((opt, txt))
                if visible_opts:
                    break
            except Exception:
                pass

        if not visible_opts:
            continue

        # Score best matching option: exact -> starts-with -> substring -> primary token -> first option
        q_low = query.lower()
        v_low = val_str.lower()
        chosen_opt, chosen_txt = visible_opts[0]
        for opt, txt in visible_opts:
            t_low = txt.lower()
            if t_low == v_low or t_low == q_low:
                chosen_opt, chosen_txt = opt, txt
                break
        else:
            for opt, txt in visible_opts:
                t_low = txt.lower()
                if t_low.startswith((q_low, v_low)):
                    chosen_opt, chosen_txt = opt, txt
                    break
            else:
                for opt, txt in visible_opts:
                    t_low = txt.lower()
                    if q_low in t_low or (primary and primary.lower() in t_low):
                        chosen_opt, chosen_txt = opt, txt
                        break

        try:
            await chosen_opt.click(timeout=3000)
            await page.wait_for_timeout(150)
            return chosen_txt
        except Exception:
            try:
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(150)
                return chosen_txt
            except Exception:
                pass

    # Fallback: press Enter on whatever was typed
    try:
        await page.keyboard.press("Enter")
        return val_str
    except Exception:
        return None


async def smart_fill_form(page, job: dict, profile: dict) -> dict:
    """Executes DOM-scoped smart form fill with React-Select combobox support (never touches nav/search bars)."""
    resolved_prof = resolve_candidate_profile(profile)
    target = await dismiss_popups_and_open_form(page)
    schema = await extract_form_schema(target)
    plan = await plan_form_fill_with_llm(schema, job, resolved_prof)

    fields_filled: dict[str, str] = {}

    for item in schema:
        fid = item["field_id"]
        val = plan.get(fid)
        if val is None or val == "":
            continue

        tag = item.get("tag")
        itype = item.get("type", "")
        selector = f'[data-relentless-id="{fid}"]'

        try:
            el = await target.query_selector(selector)
            if not el or not await el.is_visible():
                continue

            filled_val = str(val)
            if tag == "select":
                try:
                    await el.select_option(value=str(val))
                except Exception:
                    try:
                        await el.select_option(label=str(val))
                    except Exception:
                        pass
            elif itype in ("checkbox", "radio"):
                if str(val).lower() in ("true", "yes", "1", "check", "checked"):
                    if not await el.is_checked():
                        await el.check(force=True)
                else:
                    continue
            elif itype == "combobox" or tag == "combobox":
                picked = await _fill_combobox(page, target, el, str(val))
                if not picked:
                    continue
                filled_val = picked
            else:
                await el.fill(str(val))

            key_name = item.get("id") or item.get("name") or item.get("label") or fid
            fields_filled[key_name] = filled_val
        except Exception as e:
            logger.debug("Could not fill field %s: %s", fid, e)

    # Upload tailored PDF resume
    pdf_path = job.get("tailored_pdf_path") or job.get("pdf_path")
    if pdf_path and os.path.exists(pdf_path):
        try:
            file_inputs = await target.query_selector_all('input[type="file"]')
            for fi in file_inputs:
                name_attr = (await fi.get_attribute("name") or "").lower()
                id_attr = (await fi.get_attribute("id") or "").lower()
                if "cover" not in name_attr and "cover" not in id_attr:
                    await fi.set_input_files(pdf_path)
                    fields_filled["resume"] = pdf_path
                    break
            if "resume" not in fields_filled and file_inputs:
                await file_inputs[0].set_input_files(pdf_path)
                fields_filled["resume"] = pdf_path
        except Exception as e:
            logger.warning("Error uploading resume: %s", e)

    return fields_filled
