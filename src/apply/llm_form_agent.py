import json
import logging
import os
import re

import httpx

from src.httpx_utils import post_with_retry
from src.settings_store import (
    ProfileNotConfigured,
    get_master_profile,
    load_example_profile,
)

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()


def resolve_candidate_profile(profile: dict | None = None) -> dict:
    """
    Ensures a valid candidate profile is available.
    1. Uses passed profile if populated.
    2. Falls back to get_master_profile() if configured.
    3. Falls back to load_example_profile() if allowed/available.
    4. Otherwise raises ProfileNotConfigured (no hardcoded credentials allowed).
    """
    if profile and isinstance(profile, dict) and (profile.get("name") or profile.get("application_defaults")):
        # If application_defaults is missing, overlay example profile defaults
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
    """
    Stage 1:
    - Auto-dismisses visible cookie banners & GDPR overlays.
    - Clicks 'Apply for this job' / 'Apply Now' buttons if the form is hidden behind a button.
    - Resolves embedded ATS iframes (Greenhouse, Ashby, Lever, SmartRecruiters, etc.).
    """
    # 1. Dismiss cookie overlays
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
                await page.wait_for_timeout(300)
                break
        except Exception:
            pass

    # 2. Click 'Apply' / 'Apply Now' if form is behind a button
    try:
        inputs = await page.query_selector_all("input:not([type=hidden]), textarea, select")
        visible_inputs = [inp for inp in inputs if await inp.is_visible()]
    except Exception:
        visible_inputs = []

    if len(visible_inputs) == 0:
        apply_selectors = [
            "button:has-text('Apply for this job')",
            "a:has-text('Apply for this job')",
            "button:has-text('Apply Now')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply Manually')",
            "a:has-text('Apply Manually')",
            "button:has-text('Apply')",
            "a[role='button']:has-text('Apply')",
        ]
        for sel in apply_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

    # 3. Detect embedded application iframe if inputs are hosted in an iframe
    target_frame = page
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            frame_inputs = await frame.query_selector_all("input:not([type=hidden]), textarea, select")
            if len(frame_inputs) >= 2:
                target_frame = frame
                break
        except Exception:
            pass

    return target_frame


async def extract_form_schema(frame_or_page) -> list[dict]:
    """
    Stage 2:
    Injects a JS helper to index visible inputs, textareas, selects, and comboboxes.
    Assigns data-relentless-id="field_0", "field_1", ... and extracts metadata.
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

        const resolveLabel = (el) => {
            if (el.labels && el.labels.length > 0) {
                return el.labels[0].innerText.trim();
            }
            if (el.getAttribute('aria-label')) {
                return el.getAttribute('aria-label').trim();
            }
            if (el.getAttribute('aria-labelledby')) {
                const ref = document.getElementById(el.getAttribute('aria-labelledby'));
                if (ref) return ref.innerText.trim();
            }
            // Check nearest parent label or form-group container
            const parent = el.closest('label, .form-group, .field, [class*="field"], [class*="question"], tr, div');
            if (parent) {
                const lbl = parent.querySelector('label, [class*="label"], span, legend');
                if (lbl && lbl !== el) {
                    return lbl.innerText.trim();
                }
            }
            return '';
        };

        const elements = Array.from(document.querySelectorAll(
            'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([type=reset]), textarea, select, [role="combobox"]'
        )).filter(isVisible);

        return elements.map((el, index) => {
            const fieldId = `field_${index}`;
            el.setAttribute('data-relentless-id', fieldId);

            const tag = el.tagName.toLowerCase();
            const type = (el.getAttribute('type') || (tag === 'textarea' ? 'textarea' : tag)).toLowerCase();
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
    """Synthesizes a tailored motive statement using the JD context and candidate profile."""
    company = job.get("company") or "the company"
    title = job.get("title") or "this role"
    jd_text = (job.get("jd_text") or "").strip()

    # Extract company domain / mission context from the JD if available
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
    """
    Deterministic heuristic mapper sourcing strictly from profile & application defaults.
    Zero hardcoded personal data in code.
    """
    app_defaults = profile.get("application_defaults") or {}
    name_str = profile.get("name") or app_defaults.get("full_name") or ""
    parts = name_str.split()
    first_name = app_defaults.get("first_name") or (parts[0] if parts else "")
    last_name = app_defaults.get("last_name") or (parts[-1] if len(parts) > 1 else "")

    plan = {}
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

        val = None

        # 1. Contact / Identity
        if itype == "email" or "email" in key:
            val = profile.get("email") or app_defaults.get("email")
        elif itype == "tel" or "phone" in key or "mobile" in key:
            val = profile.get("phone") or app_defaults.get("phone")
        elif "first" in key and "name" in key:
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

        # 2. Screening Defaults (sourcing strictly from application_defaults / master profile)
        elif "notice" in key:
            val = app_defaults.get("notice_period") or app_defaults.get("notice_period_days")
        elif "current" in key and ("ctc" in key or "salary" in key or "compensation" in key):
            val = app_defaults.get("current_ctc_lpa")
        elif ("expected" in key or "desired" in key) and ("ctc" in key or "salary" in key or "compensation" in key):
            val = app_defaults.get("expected_ctc_lpa")
        elif "auth" in key or "legally" in key or "eligib" in key:
            val = app_defaults.get("authorized_to_work_in_india") or "Yes"
        elif "sponsorship" in key or "visa" in key:
            val = app_defaults.get("requires_visa_sponsorship") or "No"
        elif "relocat" in key:
            val = app_defaults.get("willing_to_relocate") or "Yes"
        elif "city" in key or "location" in key:
            val = app_defaults.get("current_city") or profile.get("location")
        elif "country" in key:
            val = app_defaults.get("country")
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
            val = app_defaults.get("gender")
        elif "veteran" in key:
            val = app_defaults.get("veteran_status")
        elif "disability" in key:
            val = app_defaults.get("disability_status")

        # 3. Textarea / Custom Essay Questions (synthesized dynamically from JD & profile)
        elif tag == "textarea":
            val = _generate_motive_statement(job, profile)

        # Match select options if val found
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
    """
    Stage 3:
    Uses gpt-4o-mini (cost ~$0.0009 / form) to map fields_schema to profile + screening answers.
    Leverages JD context and company details to craft authentic answers for essay questions.
    Falls back cleanly to heuristic plan when API keys are not present.
    """
    resolved_prof = resolve_candidate_profile(profile)
    heuristic_plan = _heuristic_plan_fill(fields_schema, job, resolved_prof)

    api_key = OPENAI_API_KEY or OPENROUTER_API_KEY
    if not api_key or not fields_schema:
        return heuristic_plan

    llm_url = (
        "https://api.openai.com/v1/chat/completions"
        if OPENAI_API_KEY
        else "https://openrouter.ai/api/v1/chat/completions"
    )
    model = "gpt-4o-mini"

    app_defaults = resolved_prof.get("application_defaults") or {}
    clean_profile = {
        "name": resolved_prof.get("name"),
        "email": resolved_prof.get("email"),
        "phone": resolved_prof.get("phone"),
        "location": resolved_prof.get("location"),
        "linkedin": resolved_prof.get("linkedin"),
        "github": resolved_prof.get("github"),
        "experience_summary": resolved_prof.get("experience_summary"),
        "experience": resolved_prof.get("experience", [])[:2],
        "core_skills": resolved_prof.get("core_skills", [])[:12],
        "application_defaults": app_defaults,
    }

    prompt = {
        "job": {
            "company": job.get("company"),
            "title": job.get("title"),
            "jd_text_context": (job.get("jd_text") or "")[:2000],
            "apply_url": job.get("apply_url"),
        },
        "candidate_profile": clean_profile,
        "form_fields": fields_schema,
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await post_with_retry(
                client,
                llm_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are an intelligent Job Application Autofill Agent. "
                                "Given a list of form fields, candidate profile, and job description context, "
                                "generate a JSON mapping from field_id to the exact string value to fill or option value to select. "
                                "For essay / motivation questions (e.g. 'Why do you want to join?', 'Tell us about a project'): "
                                "reference the company's mission/domain from the JD context and provide a professional, authentic 2-3 sentence answer. "
                                "Return ONLY a JSON object: {\"field_0\": \"value\", ...}"
                            ),
                        },
                        {"role": "user", "content": json.dumps(prompt)},
                    ],
                    "temperature": 0.0,
                    "response_format": {"type": "json_object"},
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    merged = dict(heuristic_plan)
                    merged.update(parsed)
                    return merged
    except Exception as e:
        logger.warning("LLM form planning failed, falling back to heuristics: %s", e)

    return heuristic_plan


async def smart_fill_form(page, job: dict, profile: dict) -> dict:
    """
    Stage 4:
    Executes the 4-stage smart form fill:
    1. Dismiss popups & resolve iframe
    2. Extract live DOM schema
    3. Plan actions with LLM + heuristics
    4. Fill text inputs, selects, radios, and upload tailored PDF resume
    """
    resolved_prof = resolve_candidate_profile(profile)
    target = await dismiss_popups_and_open_form(page)
    schema = await extract_form_schema(target)
    plan = await plan_form_fill_with_llm(schema, job, resolved_prof)

    fields_filled = {}

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

            if tag == "select":
                try:
                    await el.select_option(value=str(val))
                except Exception:
                    try:
                        await el.select_option(label=str(val))
                    except Exception:
                        pass
            elif itype in ("checkbox", "radio"):
                if str(val).lower() in ("true", "yes", "1", "check"):
                    await el.check()
            elif itype == "combobox" or tag == "combobox":
                await el.click()
                await el.fill(str(val))
                await page.keyboard.press("Enter")
            else:
                await el.fill(str(val))

            key_name = item.get("name") or item.get("id") or item.get("label") or fid
            fields_filled[key_name] = str(val)
        except Exception as e:
            logger.debug("Could not fill field %s: %s", fid, e)

    # Upload PDF Resume
    pdf_path = job.get("tailored_pdf_path") or job.get("pdf_path")
    if pdf_path and os.path.exists(pdf_path):
        try:
            file_inputs = await target.query_selector_all('input[type="file"]')
            for fi in file_inputs:
                name_attr = (await fi.get_attribute("name") or "").lower()
                id_attr = (await fi.get_attribute("id") or "").lower()
                # Prioritize resume over cover letter
                if "cover" not in name_attr and "cover" not in id_attr:
                    await fi.set_input_files(pdf_path)
                    fields_filled["resume"] = pdf_path
                    break
            # Fallback to first file input if not matched
            if "resume" not in fields_filled and file_inputs:
                await file_inputs[0].set_input_files(pdf_path)
                fields_filled["resume"] = pdf_path
        except Exception as e:
            logger.warning("Error uploading resume: %s", e)

    return fields_filled
