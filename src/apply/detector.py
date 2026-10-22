
LOGIN_MARKERS = ["password", "sign in to apply", "log in to apply", "create an account"]
CAPTCHA_MARKERS = ["hcaptcha", "recaptcha", "turnstile", "cf-turnstile"]


def _playwright():
    try:
        from playwright.async_api import async_playwright
        return async_playwright
    except Exception:
        return None


async def classify(url: str):
    ap = _playwright()
    if ap is None:
        return ("BLOCKED", "playwright not installed")
    try:
        async with ap() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=30000)
            html = await page.content()
            low = html.lower()
            if any(m in low for m in CAPTCHA_MARKERS):
                await browser.close()
                return ("CAPTCHA", "captcha detected")
            if any(m in low for m in LOGIN_MARKERS):
                await browser.close()
                return ("NEEDS_LOGIN", "login marker detected")
            # form field count
            inputs = await page.query_selector_all("input, textarea, select")
            free_text = await page.query_selector_all("textarea")
            if len(free_text) > 0:
                await browser.close()
                return ("COMPLEX", "free-text questions present")
            if len(inputs) > 25:
                await browser.close()
                return ("COMPLEX", "too many form fields")
            await browser.close()
            return ("AUTO_SIMPLE", "simple public form")
    except Exception as e:
        return ("BLOCKED", f"error: {e}")