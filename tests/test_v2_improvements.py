import os
import pathlib
import pytest
import respx
import httpx
from playwright.async_api import async_playwright

from src.ingestors.ats_poller import (
    matches_basic_filters,
    _norm_loc,
    poll_smartrecruiters,
    poll_recruitee,
    poll_remotive,
    poll_lever,
)
from src.apply.detector import classify
from src.apply.autofill import fill
from src.tailor.resume_tailor import (
    TailoredResumeOutput,
    SkillCategory,
    render_typst_pdf,
)
from src.hitl.telegram_bot import _find_job_for_message, build_inline_keyboard
from src.knowledge.distiller import run_distill, _redact
from src.knowledge.store import get_store

FIX = pathlib.Path(__file__).parent / "fixtures"


# ==============================================================================
# 1. Location Filter Unit Tests (Proves Bug Fix: Dublin/Austin != India)
# ==============================================================================
def test_location_normalization_and_word_boundaries():
    # India hub normalization
    assert "bangalore" in _norm_loc("Bengaluru")
    assert "gurgaon" in _norm_loc("Gurugram")
    assert "delhi" in _norm_loc("New Delhi")
    assert "hyderabad" in _norm_loc("Secunderabad")
    assert "mumbai" in _norm_loc("Navi Mumbai")
    assert "india" in _norm_loc("Pune, IN")
    assert "india" in _norm_loc("Hyderabad, India")

    # Anti-false-positive checks: "in" should NOT match substrings inside unrelated cities!
    assert "dublindia" not in _norm_loc("Dublin")
    assert "austindia" not in _norm_loc("Austin")
    assert "berlindia" not in _norm_loc("Berlin")


def test_matches_basic_filters_india_and_remote():
    # Allowed locations must pass
    assert matches_basic_filters("AI Engineer", "Hyderabad, India") is True
    assert matches_basic_filters("Machine Learning Engineer", "Bengaluru") is True
    assert matches_basic_filters("Python Backend Engineer", "Gurugram") is True
    assert matches_basic_filters("Computer Vision Engineer", "Pune, IN") is True
    assert matches_basic_filters("MLOps Engineer", "Remote") is True
    assert matches_basic_filters("AI Engineer", "Anywhere") is True
    assert matches_basic_filters("LLM Engineer", "Work From Home") is True

    # International locations WITHOUT India must fail (Dublin, Austin, etc.)
    assert matches_basic_filters("AI Engineer", "Dublin, Ireland") is False
    assert matches_basic_filters("AI Engineer", "Austin, TX") is False
    assert matches_basic_filters("AI Engineer", "Berlin, Germany") is False
    assert matches_basic_filters("AI Engineer", "Washington, DC") is False

    # US-only remote restrictions must fail
    assert matches_basic_filters("AI Engineer", "Remote - US Only") is False
    assert matches_basic_filters("AI Engineer", "Remote (United States Only)") is False
    assert matches_basic_filters("AI Engineer", "Remote - Canada Only") is False


# ==============================================================================
# 2. Multi-ATS Poller Tests (SmartRecruiters, Recruitee, Remotive, Lever)
# ==============================================================================
@pytest.mark.asyncio
async def test_poll_smartrecruiters_flow(monkeypatch):
    monkeypatch.setattr("src.ingestors.ats_poller.insert_job", lambda *a, **k: True)
    monkeypatch.setattr("src.ingestors.ats_poller.job_exists", lambda *a, **k: False)
    monkeypatch.setattr("src.ingestors.ats_poller.is_near_dup", lambda *a, **k: False)

    list_payload = {
        "content": [
            {
                "id": "job123",
                "name": "Machine Learning Engineer",
                "location": {"city": "Hyderabad", "country": "IN", "remote": True},
            }
        ]
    }
    detail_payload = {
        "applyUrl": "https://jobs.smartrecruiters.com/apply/job123",
        "jobAd": {
            "sections": {
                "jobDescription": {"text": "Join our AI team building agentic RAG systems."},
                "qualifications": {"text": "3+ years Python & PyTorch experience."},
            }
        },
    }

    with respx.mock:
        respx.get("https://api.smartrecruiters.com/v1/companies/testco/postings").mock(
            return_value=httpx.Response(200, json=list_payload)
        )
        respx.get("https://api.smartrecruiters.com/v1/companies/testco/postings/job123").mock(
            return_value=httpx.Response(200, json=detail_payload)
        )
        async with httpx.AsyncClient() as client:
            n = await poll_smartrecruiters(client, None, "testco")
    assert n == 1


@pytest.mark.asyncio
async def test_poll_recruitee_flow(monkeypatch):
    monkeypatch.setattr("src.ingestors.ats_poller.insert_job", lambda *a, **k: True)
    monkeypatch.setattr("src.ingestors.ats_poller.job_exists", lambda *a, **k: False)
    monkeypatch.setattr("src.ingestors.ats_poller.is_near_dup", lambda *a, **k: False)

    offers_payload = {
        "offers": [
            {
                "title": "Computer Vision Engineer",
                "location": "Bengaluru",
                "country": "IN",
                "remote": False,
                "description": "<p>YOLOv8 & TensorRT edge deployment</p>",
                "careers_url": "https://careers.recruitee.com/cv-role",
            }
        ]
    }

    with respx.mock:
        respx.get("https://testrec.recruitee.com/api/offers/").mock(
            return_value=httpx.Response(200, json=offers_payload)
        )
        async with httpx.AsyncClient() as client:
            n = await poll_recruitee(client, None, "testrec")
    assert n == 1


@pytest.mark.asyncio
async def test_poll_remotive_flow(monkeypatch):
    monkeypatch.setattr("src.ingestors.ats_poller.insert_job", lambda *a, **k: True)
    monkeypatch.setattr("src.ingestors.ats_poller.job_exists", lambda *a, **k: False)
    monkeypatch.setattr("src.ingestors.ats_poller.is_near_dup", lambda *a, **k: False)

    remotive_payload = {
        "jobs": [
            {
                "title": "Senior AI Engineer",
                "company_name": "RemotiveAI",
                "candidate_required_location": "Worldwide",
                "description": "<p>LangGraph multi-agent LLM systems</p>",
                "url": "https://remotive.com/job/123",
            }
        ]
    }

    with respx.mock:
        respx.get("https://remotive.com/api/remote-jobs?category=software-dev").mock(
            return_value=httpx.Response(200, json=remotive_payload)
        )
        async with httpx.AsyncClient() as client:
            n = await poll_remotive(client, None)
    assert n == 1


@pytest.mark.asyncio
async def test_poll_lever_workplace_and_secondary_locations(monkeypatch):
    monkeypatch.setattr("src.ingestors.ats_poller.insert_job", lambda *a, **k: True)
    monkeypatch.setattr("src.ingestors.ats_poller.job_exists", lambda *a, **k: False)
    monkeypatch.setattr("src.ingestors.ats_poller.is_near_dup", lambda *a, **k: False)

    lever_payload = [
        {
            "text": "Machine Learning Engineer",
            "categories": {
                "location": "San Francisco",
                "allLocations": ["San Francisco", "Hyderabad, India"],
            },
            "workplaceType": "hybrid",
            "descriptionPlain": "Building CV models",
            "hostedUrl": "https://jobs.lever.co/test/1",
        }
    ]

    with respx.mock:
        respx.get("https://api.lever.co/v0/postings/testlever?mode=json").mock(
            return_value=httpx.Response(200, json=lever_payload)
        )
        async with httpx.AsyncClient() as client:
            n = await poll_lever(client, None, "testlever")
    assert n == 1


# ==============================================================================
# 3. Smart DOM-Scoped Website Detector & Autofill Tests
# ==============================================================================
@pytest.mark.asyncio
async def test_detector_classifies_mock_ats_form_with_cover_letter():
    form_path = (FIX / "mock_ats_form.html").resolve()
    url = f"file://{form_path}"
    status, reason = await classify(url)
    # Must be AUTO_SIMPLE because cover letter textarea is optional, not COMPLEX!
    assert status == "AUTO_SIMPLE"
    assert "public application form ready" in reason


@pytest.mark.asyncio
async def test_detector_classifies_mock_ats_login_as_needs_login():
    login_path = (FIX / "mock_ats_login.html").resolve()
    url = f"file://{login_path}"
    status, reason = await classify(url)
    assert status == "NEEDS_LOGIN"
    assert "login wall" in reason


@pytest.mark.asyncio
async def test_autofill_populates_full_name_and_resume(tmp_path):
    # Create dummy pdf
    dummy_pdf = tmp_path / "resume.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 dummy pdf bytes")

    profile = {
        "name": "Rohan Vailala Thoma",
        "email": "rohan@example.com",
        "phone": "+91-9999999999",
        "location": "Hyderabad",
        "linkedin": "linkedin.com/in/rohan",
        "github": "github.com/rohan",
    }
    job = {
        "id": 1,
        "company": "MockCo",
        "tailored_pdf_path": str(dummy_pdf),
    }

    form_path = (FIX / "mock_ats_form.html").resolve()
    url = f"file://{form_path}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url)

        filled = await fill(page, job, profile)

        # Full name was filled into input name="full_name"
        name_val = await page.input_value('input[name="full_name"]')
        email_val = await page.input_value('input[name="email"]')

        assert name_val == "Rohan Vailala Thoma"
        assert email_val == "rohan@example.com"
        assert "resume" in filled

        await browser.close()


# ==============================================================================
# 4. Smart Resume Tailor & Typst Rendering Tests
# ==============================================================================
def test_tailored_resume_output_schema_with_categories():
    data = {
        "tailored_summary": "Staff AI Engineer with proven impact building LangGraph agents and TensorRT edge pipelines.",
        "matched_keywords": ["LangGraph", "RAG", "TensorRT", "YOLOv8"],
        "skill_categories": [
            {
                "category": "GenAI & Agentic Systems",
                "skills": ["LangGraph", "RAG", "LangSmith", "Bedrock"],
            },
            {
                "category": "Computer Vision & Edge AI",
                "skills": ["YOLOv8", "TensorRT", "DeepStream", "ONNX"],
            },
        ],
        "tailored_bullets_role_1": ["Engineered enterprise LangGraph RAG system..."],
        "tailored_bullets_role_2": ["Deployed YOLOv8 models on edge devices with TensorRT..."],
        "tailored_bullets_role_3": ["Developed forklift safety tracking with 98% accuracy..."],
        "change_rationale": ["Framed bullets around high-throughput edge latency and multi-agent coordination."],
    }
    out = TailoredResumeOutput(**data)
    assert len(out.skill_categories) == 2
    assert len(out.tailored_bullets_role_3) == 1


def test_render_typst_pdf_generates_valid_pdf():
    tailored_data = {
        "tailored_summary": "Experienced ML Engineer specializing in Agentic RAG and Real-Time Computer Vision.",
        "matched_keywords": ["Python", "FastAPI", "YOLO", "TensorRT", "LangGraph"],
        "skill_categories": [
            SkillCategory(category="GenAI & Agents", skills=["LangGraph", "RAG", "LangSmith"]),
            SkillCategory(category="Computer Vision", skills=["YOLOv8", "TensorRT", "OpenCV"]),
            SkillCategory(category="Backend & MLOps", skills=["FastAPI", "Docker", "AWS"]),
        ],
        "tailored_bullets_role_1": [
            "Architected production LangGraph RAG support portal with Dynamics 365 integration.",
            "Deployed real-time industrial defect detection pipeline on Azure.",
        ],
        "tailored_bullets_role_2": [
            "Implemented document OCR combining DeepSeek-VL2 and traditional CV achieving 95% accuracy.",
            "Built YOLOv8 edge inference service with TensorRT running at 15 FPS on Jetson Nano.",
        ],
        "tailored_bullets_role_3": [
            "Developed automated forklift detection and tracking system with 98% precision.",
        ],
        "change_rationale": ["Highlighted edge deployments and categorized skills cleanly."],
    }
    pdf_path = render_typst_pdf("AcmeAI", "Senior Machine Learning Engineer", tailored_data)
    assert os.path.exists(pdf_path)
    assert os.path.getsize(pdf_path) > 1000  # Valid compiled PDF


# ==============================================================================
# 5. Async Telegram Message Resolution & OKF Distiller Tests
# ==============================================================================
def test_find_job_for_message_regex_fallback():
    reply_msg_1 = {"text": "Here is your PDF for Job #42 (Stripe — AI Engineer)"}
    assert _find_job_for_message(reply_msg_1) == 42

    reply_msg_2 = {"caption": "Review Package: Job #108\nWebsite Check: Direct form ready"}
    assert _find_job_for_message(reply_msg_2) == 108


def test_build_inline_keyboard_structure():
    kb = build_inline_keyboard(99)
    rows = kb["inline_keyboard"]
    assert len(rows) == 2
    assert rows[0][0]["callback_data"] == "interest:99"
    assert rows[0][1]["callback_data"] == "act:SKIP_BAD_FIT:99"


def test_distiller_redact():
    assert _redact("Contact me at user@test.com") == "Contact me at [email]"
    assert _redact("Call +91 9876543210 today") == "Call [phone] today"


def test_run_distill_creates_market_and_feedback_concepts(monkeypatch):
    sample_jobs = [
        {
            "id": 1,
            "source_ats": "greenhouse",
            "company": "Stripe",
            "title": "Staff AI Engineer",
            "location": "Hyderabad, India",
            "jd_text": "Building LangGraph multi-agent RAG systems with FastAPI and Docker.",
            "human_action": "APPLIED",
            "jev_persona": "ai_agent_engineer",
            "user_edit_notes": ["Highlight LangSmith observability"],
        },
        {
            "id": 2,
            "source_ats": "lever",
            "company": "GhostAI",
            "title": "Junior Developer",
            "location": "Remote",
            "jd_text": "Unpaid trial project",
            "human_action": "SKIP_SCAM",
            "jev_persona": "backend_python_engineer",
            "user_edit_notes": [],
        },
    ]

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def execute(self, query, params=None):
            self.query = query

        def fetchall(self):
            if "okf_concepts" in self.query or "knowledge_forget" in self.query:
                return []
            if "human_action IS NOT NULL" in self.query:
                return [j for j in sample_jobs if j.get("human_action")]
            if "user_edit_notes IS NOT NULL" in self.query:
                return [j for j in sample_jobs if j.get("user_edit_notes")]
            return sample_jobs

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

    monkeypatch.setattr("src.knowledge.distiller.get_connection", lambda: FakeConn())
    monkeypatch.setattr("src.knowledge.store.get_connection", lambda: FakeConn())

    res = run_distill()
    assert isinstance(res, dict)
    store = get_store()
    hot = store.get("market/hot-skills")
    assert hot is not None
    assert hot.concept_type == "Market Intelligence"
    assert "langgraph" in hot.body.lower()

    edits = store.get("preferences/resume-edit-habits")
    assert edits is not None
    assert "LangSmith" in edits.body
