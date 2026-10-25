import pathlib

import httpx
import pytest
import respx
from playwright.async_api import async_playwright

from src.apply.autofill import fill
from src.apply.detector import classify
from src.ingestors.gmail_ingestor import (
    extract_job_links_from_email_html,
    fetch_linkedin_guest_job,
)
from src.ingestors.url_resolver import (
    extract_ats_board_from_url,
    resolve_direct_apply_url,
)
from src.tailor.ats_scorer import compute_ats_scorecard, format_scorecard_markdown

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


# ==============================================================================
# Upgrade 1: Dynamic ATS & Workday URL Discoverer Test
# ==============================================================================
def test_v3_dynamic_ats_and_workday_url_discoverer():
    # Workday tenant, instance, and site extraction
    wd_url = "https://qualcomm.wd5.myworkdayjobs.com/en-US/External/job/Hyderabad/ML-Engineer_R123"
    wd_res = extract_ats_board_from_url(wd_url)
    assert wd_res is not None
    assert wd_res["platform"] == "workday_cxs"
    assert wd_res["slug"] == "qualcomm:wd5:External"
    assert wd_res["company"] == "Qualcomm"
    assert wd_res["metadata"]["tenant"] == "qualcomm"
    assert wd_res["metadata"]["wd_instance"] == "wd5"
    assert wd_res["metadata"]["site"] == "External"

    # Greenhouse board extraction
    gh_url = "https://boards.greenhouse.io/anthropic/jobs/998877"
    gh_res = extract_ats_board_from_url(gh_url)
    assert gh_res == {
        "platform": "greenhouse",
        "slug": "anthropic",
        "company": "Anthropic",
    }

    # Lever site extraction
    lever_url = "https://jobs.lever.co/cohere/112233"
    lever_res = extract_ats_board_from_url(lever_url)
    assert lever_res == {
        "platform": "lever",
        "slug": "cohere",
        "company": "Cohere",
    }

    # Ashby board extraction
    ashby_url = "https://jobs.ashbyhq.com/perplexity/445566"
    ashby_res = extract_ats_board_from_url(ashby_url)
    assert ashby_res == {
        "platform": "ashby",
        "slug": "perplexity",
        "company": "Perplexity",
    }

    # SmartRecruiters board extraction
    sr_url = "https://jobs.smartrecruiters.com/LinkedIn3/778899"
    sr_res = extract_ats_board_from_url(sr_url)
    assert sr_res == {
        "platform": "smartrecruiters",
        "slug": "LinkedIn3",
        "company": "LinkedIn3",
    }


# ==============================================================================
# Upgrade 2: Cookie Popup + Dropdown + Screening Question Form Agent Test
# ==============================================================================
@pytest.mark.asyncio
async def test_v3_cookie_dropdown_screening_form_agent(tmp_path):
    dummy_pdf = tmp_path / "tailored_resume.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 dummy pdf bytes for test")

    complex_html = (FIXTURES_DIR / "mock_complex_ats_form.html").resolve()
    url = f"file://{complex_html}"

    # 1. Detector classifies complex form as AUTO_SIMPLE (handling 1 required essay)
    status, reason = await classify(url)
    assert status == "AUTO_SIMPLE"
    assert "public application form ready" in reason

    # 2. Autofill dismisses cookie overlay and fills custom inputs & dropdowns
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
        "id": 99,
        "company": "RoboticsAI",
        "title": "Machine Learning Engineer",
        "tailored_pdf_path": str(dummy_pdf),
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url)

        # Cookie overlay is initially present
        cookie_el = await page.query_selector("#onetrust-accept-btn-handler")
        assert cookie_el is not None

        # Execute autofill
        filled = await fill(page, job, profile)

        # Cookie overlay was dismissed
        assert not await cookie_el.is_visible()

        # Input values filled properly
        assert await page.input_value("#first_name") == "Rohan"
        assert await page.input_value("#last_name") == "Thoma"
        assert await page.input_value("#email") == "rohan@example.com"
        assert await page.input_value("#phone") == "+91-7013313694"
        assert await page.input_value("#work_auth") == "Yes"
        assert "30 days" in (await page.input_value("#notice_period"))

        essay_val = await page.input_value("#essay_why")
        assert len(essay_val) > 10

        assert "resume" in filled

        await browser.close()


# ==============================================================================
# Upgrade 3: Gmail Alert Parser & LinkedIn Guest JD Extractor Test
# ==============================================================================
@pytest.mark.asyncio
async def test_v3_gmail_alert_parser_and_linkedin_guest_jd():
    mock_email_html = """
    <div>
      <p>Recommended jobs:</p>
      <a href="https://www.linkedin.com/comm/jobs/view/4099887766">Machine Learning Engineer</a>
    </div>
    """
    links = extract_job_links_from_email_html(mock_email_html)
    assert links["linkedin_job_ids"] == ["4099887766"]

    mock_guest_html = """
    <div class="topcard">
      <h1 class="top-card-layout__title">AI Engineer - Computer Vision</h1>
      <a class="topcard__org-name-link">VisionTech India</a>
      <span class="topcard__flavor topcard__flavor--bullet">Hyderabad, India</span>
      <div class="show-more-less-html__markup">
        <p>Requirements: Deep experience with PyTorch, OpenCV, TensorRT, and CUDA acceleration.</p>
      </div>
      <a class="apply-button" href="https://www.linkedin.com/jobs/view/externalApply/123?url=https%3A%2F%2Fjobs.lever.co%2Fvisiontech%2F9988">Apply</a>
    </div>
    """

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4099887766").respond(
                status_code=200, html=mock_guest_html
            )
            job_data = await fetch_linkedin_guest_job(client, "4099887766")
            assert job_data is not None
            assert job_data["title"] == "AI Engineer - Computer Vision"
            assert job_data["company"] == "VisionTech India"
            assert "Hyderabad" in job_data["location"]
            assert "TensorRT" in job_data["jd_text"]
            assert job_data["external_apply_url"] == "https://jobs.lever.co/visiontech/9988"

            # Direct apply URL resolution
            resolved = await resolve_direct_apply_url(
                client,
                job_data["company"],
                job_data["title"],
                job_data["external_apply_url"],
            )
            assert resolved == "https://jobs.lever.co/visiontech/9988"


# ==============================================================================
# Upgrade 4: Quantitative Mock ATS Scorer Test
# ==============================================================================
def test_v3_mock_ats_scorer():
    jd_text = """
    Role: Senior Machine Learning Engineer
    Requirements:
    - 3+ years experience with PyTorch, Python, Docker, FastAPI, and Kubernetes.
    - Deep knowledge of latency profiling, inference acceleration, and vector databases.
    - Hands-on experience building distributed data pipelines.
    """

    raw_profile = {
        "core_skills": ["Python", "PyTorch", "FastAPI", "Docker", "SQL"],
        "experience_summary": "Machine Learning Engineer with experience in Python and PyTorch.",
        "experience": [
            {
                "role": "ML Engineer",
                "bullets": ["Trained neural networks using PyTorch.", "Built REST APIs with FastAPI."],
            }
        ],
    }

    tailored = {
        "tailored_summary": "Machine Learning Engineer specializing in PyTorch inference latency profiling and vector databases.",
        "matched_keywords": ["PyTorch", "Latency Profiling", "Vector Databases", "Docker", "FastAPI"],
        "skill_categories": [
            {"category": "Core ML", "skills": ["PyTorch", "Latency Profiling", "Vector Databases"]}
        ],
        "tailored_bullets_role_1": [
            "Architected low-latency PyTorch inference microservices with FastAPI, achieving 40% latency reduction and 50 FPS throughput.",
            "Engineered automated containerized pipelines with Docker, scaling to 10M+ daily events.",
        ],
        "tailored_bullets_role_2": [],
        "tailored_bullets_role_3": [],
        "change_rationale": ["Added latency profiling and quantified bullet metrics"],
    }

    scorecard = compute_ats_scorecard(jd_text, raw_profile, tailored)

    assert scorecard["overall_after_pct"] > scorecard["overall_before_pct"]
    assert scorecard["boost_pct"] > 0
    assert scorecard["hard_skills_after_pct"] >= scorecard["hard_skills_before_pct"]
    assert scorecard["impact_density_pct"] >= 90
    assert any("Latency" in t or "Vector" in t for t in scorecard["newly_added_terms"])
    assert any("Kubernetes" in m for m in scorecard["missing_jd_skills"])

    formatted_md = format_scorecard_markdown(scorecard)
    assert "Mock ATS Match Scorecard" in formatted_md
    assert f"{scorecard['overall_before_pct']}% (Raw) ➔ {scorecard['overall_after_pct']}% (Tailored)" in formatted_md
    assert f"[+{scorecard['boost_pct']}% Boost]" in formatted_md
