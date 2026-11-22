"""Phase 3 tests: ETag/304 ATS caching, Workday multi-location & bounded pagination,
3-stage Career-Site Resolver, public hiring email extraction, and Gmail direct ATS ingestion.
"""

import json

import httpx
import pytest
import respx

from src.ingestors.ats_poller import poll_greenhouse, poll_workday
from src.ingestors.gmail_ingestor import extract_job_links_from_email_html, fetch_direct_ats_job
from src.ingestors.url_resolver import (
    extract_public_hiring_email,
    resolve_direct_apply_url,
)


def test_extract_public_hiring_email():
    jd = "Send portfolio to careers@awesomeai.dev or check privacy@awesomeai.dev or no-reply@linkedin.com."
    assert extract_public_hiring_email(jd) == "careers@awesomeai.dev"
    assert extract_public_hiring_email("Contact no-reply@example.com only") is None


@pytest.mark.asyncio
async def test_greenhouse_etag_304_short_circuits(monkeypatch):
    monkeypatch.setattr(
        "src.ingestors.ats_poller._get_board_health",
        lambda plat, slug: {"active": True, "etag": '"etag-v1"', "next_poll_at": None},
    )
    recorded = []
    monkeypatch.setattr(
        "src.ingestors.ats_poller._record_board_health",
        lambda *args, **kwargs: recorded.append(args),
    )

    with respx.mock:
        route = respx.get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true").mock(
            return_value=httpx.Response(304)
        )
        async with httpx.AsyncClient() as client:
            n = await poll_greenhouse(client, None, "stripe")

    assert n == 0
    assert route.calls.last.request.headers.get("If-None-Match") == '"etag-v1"'
    assert recorded and recorded[0][2] == 304


@pytest.mark.asyncio
async def test_workday_resolves_multi_location_placeholder_and_avoids_lucene_or(monkeypatch):
    inserted = []
    monkeypatch.setattr("src.ingestors.ats_poller.insert_job", lambda *a, **k: inserted.append((a, k)) or True)
    monkeypatch.setattr("src.ingestors.ats_poller.job_exists", lambda *a, **k: False)
    monkeypatch.setattr("src.ingestors.ats_poller.is_near_dup", lambda *a, **k: False)
    monkeypatch.setattr("src.ingestors.ats_poller._workday_search_terms", lambda: ["Machine Learning"])

    captured_payloads = []

    def list_handler(request):
        captured_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "total": 1,
                "jobPostings": [
                    {
                        "title": "Machine Learning Engineer",
                        "locationsText": "3 Locations",
                        "externalPath": "/job/ML-Remote/R999",
                    }
                ],
            },
        )

    detail_payload = {
        "jobPostingInfo": {
            "jobReqId": "R999",
            "location": "Remote, India",
            "additionalLocations": ["Bengaluru, India", "Remote - Worldwide"],
            "remoteType": "Remote",
            "jobDescription": "<p>Build LLM agents. Contact hiring@nvidiatest.ai</p>",
            "externalUrl": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/ML-Remote/R999",
        }
    }

    cfg = {"company": "NVIDIA", "tenant": "nvidia", "wd_instance": "wd5", "site": "NVIDIAExternalCareerSite"}
    with respx.mock:
        respx.post("https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs").mock(
            side_effect=list_handler
        )
        respx.get("https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/job/ML-Remote/R999").mock(
            return_value=httpx.Response(200, json=detail_payload)
        )
        async with httpx.AsyncClient() as client:
            n = await poll_workday(client, None, cfg, max_pages=2)

    assert n == 1
    assert " OR " not in captured_payloads[0]["searchText"]
    assert captured_payloads[0]["searchText"] == "Machine Learning"
    assert inserted[0][1]["work_mode"] == "remote"
    assert inserted[0][1]["hiring_contact_email"] == "hiring@nvidiatest.ai"


@pytest.mark.asyncio
async def test_career_resolver_uses_direct_ats_registry_before_ddg():
    with respx.mock:
        respx.get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "title": "Machine Learning Engineer, Risk",
                            "absolute_url": "https://boards.greenhouse.io/stripe/jobs/778899",
                        }
                    ]
                },
            )
        )
        async with httpx.AsyncClient() as client:
            resolved = await resolve_direct_apply_url(
                client,
                "Stripe",
                "Machine Learning Engineer, Risk",
                "https://www.linkedin.com/jobs/view/12345678",
            )
    assert resolved == "https://boards.greenhouse.io/stripe/jobs/778899"


@pytest.mark.asyncio
async def test_gmail_extracts_multi_board_links_and_direct_greenhouse_job():
    html = """
    <html><body>
      <a href="https://www.linkedin.com/jobs/view/445566">LI</a>
      <a href="https://boards.greenhouse.io/stripe/jobs/991122">GH</a>
      <a href="https://www.naukri.com/job-listings-ai-engineer-acme-12345">Naukri</a>
      <a href="https://in.indeed.com/viewjob?jk=abcdef123">Indeed</a>
      <a href="https://wellfound.com/jobs/98765-ml-engineer">Wellfound</a>
    </body></html>
    """
    links = extract_job_links_from_email_html(html)
    assert links["linkedin_job_ids"] == ["445566"]
    assert links["direct_ats_urls"] == ["https://boards.greenhouse.io/stripe/jobs/991122"]
    assert len(links["other_job_urls"]) == 3

    with respx.mock:
        respx.get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs/991122").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 991122,
                    "title": "AI Engineer",
                    "location": {"name": "Remote, Worldwide"},
                    "content": "<p>Python & LangGraph</p>",
                    "absolute_url": "https://boards.greenhouse.io/stripe/jobs/991122",
                },
            )
        )
        async with httpx.AsyncClient() as client:
            job = await fetch_direct_ats_job(client, "https://boards.greenhouse.io/stripe/jobs/991122")
    assert job is not None
    assert job["company"] == "Stripe"
    assert job["title"] == "AI Engineer"
    assert job["external_job_id"] == "991122"
