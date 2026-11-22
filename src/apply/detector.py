from __future__ import annotations

import logging
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from src.apply.llm_form_agent import dismiss_popups_and_open_form
from src.config import load_config

logger = logging.getLogger(__name__)

# Portals that require authenticated accounts or block automated headless submissions.
MANUAL_ONLY_HOSTS: tuple[str, ...] = (
    "linkedin.com",
    "naukri.com",
    "indeed.com",
    "glassdoor.com",
    "myworkdayjobs.com",
    "instahyre.com",
    "wellfound.com",
)


def _playwright():
    return async_playwright


def detect_aggregator_shell(url: str) -> tuple[str, str] | None:
    """Fast URL pre-flight check so headless Playwright never wastes time on LinkedIn/Naukri/Workday shells."""
    if not url:
        return ("MANUAL_APPLY", "missing application URL")
    host = (urlparse(url).hostname or "").lower()
    for blocked in MANUAL_ONLY_HOSTS:
        if host == blocked or host.endswith("." + blocked):
            return (
                "MANUAL_APPLY",
                f"Portal ({host}) requires user account/login — open direct URL and upload tailored PDF manually.",
            )
    return None


async def _analyze_page_dom(page) -> tuple[str, str]:
    """Inspects live DOM strictly inside non-navigation application regions.

    Never misclassifies site-wide search bars (<nav>/<header>) or bare 'Apply' marketing links
    as ready application forms, and strictly enforces config.automation.max_free_text_questions.
    """
    try:
        max_free_text = int(load_config().automation.max_free_text_questions)
    except Exception:
        max_free_text = 2

    info = await page.evaluate(
        """() => {
        const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        };

        const isChromeOrSearch = (el) => {
            if (!el) return true;
            if (el.closest('header, nav, footer, [role="navigation"], [role="search"], [role="banner"]')) return true;
            const type = (el.getAttribute('type') || '').toLowerCase();
            if (type === 'search') return true;
            const meta = ((el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
            if (meta.includes('search') || meta.includes('keyword') || meta.includes('filter jobs')) return true;
            return false;
        };

        const bodyText = (document.body ? document.body.innerText : '').toLowerCase();

        // 1. Check for blocking CAPTCHA / WAF / Bot verification walls
        const captchaSelectors = [
            'iframe[src*="hcaptcha.com"]',
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="recaptcha/api2/bframe"]',
            '.cf-turnstile',
            '.h-captcha'
        ];
        let hasBlockingCaptcha = false;
        for (const sel of captchaSelectors) {
            for (const el of document.querySelectorAll(sel)) {
                if (isVisible(el)) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 100 && rect.height > 40) {
                        hasBlockingCaptcha = true;
                    }
                }
            }
        }
        if (
            bodyText.includes('performing security verification') ||
            bodyText.includes('verify you are human') ||
            bodyText.includes('access denied') && bodyText.includes('permission to access')
        ) {
            hasBlockingCaptcha = true;
        }

        // 2. Look strictly for genuine applicant inputs (excluding header/nav/search bars!)
        const rawInputs = Array.from(document.querySelectorAll('input, textarea, select')).filter(el => isVisible(el) && !isChromeOrSearch(el));
        const fileInputs = Array.from(document.querySelectorAll('input[type="file"]')).filter(el => !el.closest('header, nav, footer'));
        const emailInputs = rawInputs.filter(el => {
            const meta = ((el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '')).toLowerCase();
            return el.type === 'email' || meta.includes('email');
        });
        const nameInputs = rawInputs.filter(el => {
            const meta = ((el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '')).toLowerCase();
            return meta.includes('first_name') || meta.includes('firstname') || meta.includes('full_name') || meta.includes('fullname') || meta.includes('applicant');
        });
        const passwordInputs = Array.from(document.querySelectorAll('input[type="password"]')).filter(isVisible);

        // Submit buttons inside forms or main content (never nav 'Sign in')
        const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], a[role="button"]'))
            .filter(el => isVisible(el) && !el.closest('header, nav, footer'));
        const hasSubmitBtn = buttons.some(b => {
            const t = (b.innerText || b.value || '').toLowerCase();
            return b.type === 'submit' || t.includes('submit application') || t.includes('submit') || t.includes('apply');
        });

        // STRICT requirement: must have real applicant inputs (file upload OR email OR name) AND submit button!
        const hasAppForm = (fileInputs.length > 0 || emailInputs.length > 0 || nameInputs.length > 0) && hasSubmitBtn;

        // 3. Login wall detection
        const h1Text = Array.from(document.querySelectorAll('h1, h2, h3')).map(h => (h.innerText || '').toLowerCase()).join(' ');
        const explicitLoginHeading = (
            h1Text.includes('sign in to apply') ||
            h1Text.includes('log in to apply') ||
            h1Text.includes('login required') ||
            h1Text.includes('join or sign in') ||
            h1Text.includes('create an account')
        );
        const needsLogin = (!hasAppForm && passwordInputs.length > 0) || (!hasAppForm && explicitLoginHeading);

        // 4. Count required custom free-text essay textareas (excluding optional cover letter/notes)
        const textareas = rawInputs.filter(el => el.tagName.toLowerCase() === 'textarea');
        const requiredEssays = textareas.filter(ta => {
            const meta = ((ta.name || '') + ' ' + (ta.id || '') + ' ' + (ta.placeholder || '')).toLowerCase();
            const isCoverLetter = meta.includes('cover') || meta.includes('note') || meta.includes('additional');
            const isRequired = ta.required || ta.getAttribute('aria-required') === 'true';
            return isRequired && !isCoverLetter;
        });

        return {
            hasBlockingCaptcha,
            hasAppForm,
            needsLogin,
            totalVisibleInputs: rawInputs.length,
            requiredEssayCount: requiredEssays.length,
            fileInputCount: fileInputs.length,
            hasSubmitBtn
        };
    }"""
    )
    if info["hasBlockingCaptcha"]:
        return ("CAPTCHA", "blocking captcha or bot-verification challenge detected (human handoff required)")
    if info["needsLogin"]:
        return ("NEEDS_LOGIN", "login wall detected (password or account required before form)")
    if info["requiredEssayCount"] > max_free_text:
        return (
            "COMPLEX",
            f"{info['requiredEssayCount']} required essay question(s) exceed automation.max_free_text_questions ({max_free_text})",
        )
    if info["totalVisibleInputs"] > 35:
        return ("COMPLEX", f"too many form fields ({info['totalVisibleInputs']})")
    if info["hasAppForm"]:
        return ("AUTO_SIMPLE", "public application form ready")
    return ("NEEDS_REVIEW", "no direct application form inputs found on page (only landing page or external redirect)")


async def classify(url: str) -> tuple[str, str]:
    shell = detect_aggregator_shell(url)
    if shell is not None:
        return shell

    ap = _playwright()
    if ap is None:
        return ("BLOCKED", "playwright not installed")
    try:
        async with ap() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            target = await dismiss_popups_and_open_form(page)
            status, reason = await _analyze_page_dom(target)

            if status == "NEEDS_REVIEW":
                apply_btn = await target.query_selector(
                    "main a:has-text('Apply'), main button:has-text('Apply'), "
                    "button:has-text('Apply for this job'), a:has-text('Apply for this job'), "
                    "button:has-text('Apply Now'), a:has-text('Apply Now')"
                )
                if apply_btn:
                    try:
                        await apply_btn.click()
                        await page.wait_for_timeout(1500)
                        target = await dismiss_popups_and_open_form(page)
                        status, reason = await _analyze_page_dom(target)
                    except Exception:
                        pass

            await browser.close()
            return (status, reason)
    except Exception as e:
        return ("BLOCKED", f"error: {e}")
