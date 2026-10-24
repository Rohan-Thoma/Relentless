import logging

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)


def _playwright():
    return async_playwright


async def _analyze_page_dom(page) -> tuple[str, str]:
    """Inspects live DOM using page.evaluate to distinguish real login/captcha walls from navbar links."""
    info = await page.evaluate(
        """() => {
        const isVisible = (el) => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        };

        // 1. Check for visible blocking CAPTCHA iframes/widgets (ignore invisible v3 badges)
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

        // 2. Look for candidate application inputs (file upload, email, name, etc.)
        const allInputs = Array.from(document.querySelectorAll('input, textarea, select')).filter(isVisible);
        const fileInputs = Array.from(document.querySelectorAll('input[type="file"]')); // file inputs are often styled hidden
        const emailInputs = allInputs.filter(el =>
            el.type === 'email' ||
            (el.name || '').toLowerCase().includes('email') ||
            (el.placeholder || '').toLowerCase().includes('email')
        );
        const passwordInputs = allInputs.filter(el => el.type === 'password');

        // Check for submit or apply buttons
        const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], a[role="button"]')).filter(isVisible);
        const hasSubmitBtn = buttons.some(b => {
            const t = (b.innerText || b.value || '').toLowerCase();
            return b.type === 'submit' || t.includes('submit') || t.includes('apply');
        });

        const hasAppForm = (fileInputs.length > 0 || emailInputs.length > 0) && hasSubmitBtn;

        // 3. Only flag NEEDS_LOGIN if there is a visible password field AND no accessible application form,
        // or the main heading explicitly demands sign-in without any application inputs.
        const h1Text = Array.from(document.querySelectorAll('h1, h2')).map(h => (h.innerText || '').toLowerCase()).join(' ');
        const explicitLoginHeading = (h1Text.includes('sign in to apply') || h1Text.includes('log in to apply') || h1Text.includes('login required'));
        const needsLogin = (!hasAppForm && passwordInputs.length > 0) || (!hasAppForm && explicitLoginHeading);

        // 4. Count only REQUIRED custom free-text textareas (ignore optional cover letter / notes)
        const textareas = Array.from(document.querySelectorAll('textarea')).filter(isVisible);
        const requiredEssays = textareas.filter(ta => {
            const name = ((ta.name || '') + ' ' + (ta.id || '') + ' ' + (ta.placeholder || '')).toLowerCase();
            const isCoverLetter = name.includes('cover') || name.includes('note') || name.includes('additional');
            const isRequired = ta.required || ta.getAttribute('aria-required') === 'true';
            return isRequired && !isCoverLetter;
        });

        return {
            hasBlockingCaptcha,
            hasAppForm,
            needsLogin,
            totalVisibleInputs: allInputs.length,
            requiredEssayCount: requiredEssays.length,
            fileInputCount: fileInputs.length,
            hasSubmitBtn
        };
    }"""
    )
    if info["hasBlockingCaptcha"]:
        return ("CAPTCHA", "blocking captcha challenge detected")
    if info["needsLogin"]:
        return ("NEEDS_LOGIN", "login wall detected (password required before form)")
    if info["requiredEssayCount"] > 0:
        return ("COMPLEX", f"{info['requiredEssayCount']} required essay question(s) present")
    if info["totalVisibleInputs"] > 30:
        return ("COMPLEX", f"too many form fields ({info['totalVisibleInputs']})")
    if info["hasAppForm"] or info["hasSubmitBtn"]:
        return ("AUTO_SIMPLE", "public application form ready")
    return ("NEEDS_REVIEW", "no direct form inputs found on landing page")


async def classify(url: str):
    ap = _playwright()
    if ap is None:
        return ("BLOCKED", "playwright not installed")
    try:
        async with ap() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=30000)
            status, reason = await _analyze_page_dom(page)

            # If no direct form on landing page, check if there's an "Apply" button/link that opens the form
            if status == "NEEDS_REVIEW":
                apply_btn = await page.query_selector(
                    "a:has-text('Apply'), button:has-text('Apply'), [role=button]:has-text('Apply')"
                )
                if apply_btn:
                    try:
                        await apply_btn.click()
                        await page.wait_for_timeout(1500)
                        status, reason = await _analyze_page_dom(page)
                    except Exception:
                        pass

            await browser.close()
            return (status, reason)
    except Exception as e:
        return ("BLOCKED", f"error: {e}")
