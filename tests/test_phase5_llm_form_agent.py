import pathlib

import pytest
from playwright.async_api import async_playwright

from src.apply.autofill import fill
from src.apply.detector import classify
from src.apply.llm_form_agent import dismiss_popups_and_open_form, extract_form_schema

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_complex_form_detector_classification():
    complex_html = (FIXTURES_DIR / "mock_complex_ats_form.html").resolve()
    url = f"file://{complex_html}"

    status, reason = await classify(url)
    assert status == "AUTO_SIMPLE"
    assert "public application form ready" in reason


@pytest.mark.asyncio
async def test_complex_form_dismiss_popups_and_extract_schema():
    complex_html = (FIXTURES_DIR / "mock_complex_ats_form.html").resolve()
    url = f"file://{complex_html}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url)

        # Before dismissal, cookie overlay is visible
        cookie_btn = await page.query_selector("#onetrust-accept-btn-handler")
        assert cookie_btn is not None
        assert await cookie_btn.is_visible()

        # Dismiss popups
        target = await dismiss_popups_and_open_form(page)
        assert target is not None

        # After dismissal, cookie overlay should be hidden
        assert not await cookie_btn.is_visible()

        # Extract schema
        schema = await extract_form_schema(target)
        assert len(schema) >= 8

        field_names = [f["name"] for f in schema]
        assert "first_name" in field_names
        assert "last_name" in field_names
        assert "email" in field_names
        assert "work_auth" in field_names
        assert "why_join" in field_names

        # Verify select options extracted
        work_auth_field = next(f for f in schema if f["name"] == "work_auth")
        assert len(work_auth_field["options"]) >= 2

        await browser.close()


@pytest.mark.asyncio
async def test_smart_fill_form_execution(tmp_path):
    dummy_pdf = tmp_path / "tailored_resume.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 mock pdf content")

    complex_html = (FIXTURES_DIR / "mock_complex_ats_form.html").resolve()
    url = f"file://{complex_html}"

    profile = {
        "name": "Rohan Vailala Thoma",
        "email": "rohan@example.com",
        "phone": "+91-7013313694",
        "location": "Hyderabad, India",
        "linkedin": "linkedin.com/in/rohan-thoma",
        "github": "github.com/rohan-thoma",
        "application_defaults": {
            "first_name": "Rohan",
            "last_name": "Thoma",
            "full_name": "Rohan Vailala Thoma",
            "current_company": "Kynea Solutions LLP",
            "notice_period": "30 days",
            "authorized_to_work_in_india": "Yes",
        },
    }

    job = {
        "id": 42,
        "company": "DeepVision AI",
        "title": "Senior ML Engineer",
        "tailored_pdf_path": str(dummy_pdf),
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url)

        filled = await fill(page, job, profile)

        first_val = await page.input_value("#first_name")
        last_val = await page.input_value("#last_name")
        email_val = await page.input_value("#email")
        phone_val = await page.input_value("#phone")
        auth_val = await page.input_value("#work_auth")
        notice_val = await page.input_value("#notice_period")
        why_val = await page.input_value("#essay_why")

        assert first_val == "Rohan"
        assert last_val == "Thoma"
        assert email_val == "rohan@example.com"
        assert phone_val == "+91-7013313694"
        assert auth_val == "Yes"
        assert "30 days" in notice_val
        assert len(why_val) > 10
        assert "resume" in filled

        await browser.close()

