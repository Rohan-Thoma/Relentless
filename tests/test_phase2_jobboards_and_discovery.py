import httpx
import pandas as pd
import pytest
import respx

from src.ingestors.ats_poller import load_discovered_boards
from src.ingestors.jobboards_poller import get_jobboards_parameters, poll_jobboards
from src.ingestors.url_resolver import (
    extract_ats_board_from_url,
    register_discovered_board,
    resolve_direct_apply_url,
)


def test_extract_ats_board_from_url():
    # Workday with locale
    wd_res = extract_ats_board_from_url(
        "https://qualcomm.wd5.myworkdayjobs.com/en-US/External/job/Hyderabad/ML-Engineer_R123"
    )
    assert wd_res is not None
    assert wd_res["platform"] == "workday_cxs"
    assert wd_res["slug"] == "qualcomm:wd5:External"
    assert wd_res["company"] == "Qualcomm"
    assert wd_res["metadata"]["tenant"] == "qualcomm"
    assert wd_res["metadata"]["wd_instance"] == "wd5"
    assert wd_res["metadata"]["site"] == "External"

    # Workday CXS format
    wd_cxs = extract_ats_board_from_url(
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    )
    assert wd_cxs is not None
    assert wd_cxs["slug"] == "nvidia:wd5:NVIDIAExternalCareerSite"

    # Greenhouse
    gh_res = extract_ats_board_from_url("https://boards.greenhouse.io/anthropic/jobs/12345")
    assert gh_res == {
        "platform": "greenhouse",
        "slug": "anthropic",
        "company": "Anthropic",
    }

    # Lever
    lever_res = extract_ats_board_from_url("https://jobs.lever.co/cohere/67890")
    assert lever_res == {
        "platform": "lever",
        "slug": "cohere",
        "company": "Cohere",
    }

    # Ashby
    ashby_res = extract_ats_board_from_url("https://jobs.ashbyhq.com/perplexity/abcde")
    assert ashby_res == {
        "platform": "ashby",
        "slug": "perplexity",
        "company": "Perplexity",
    }

    # SmartRecruiters
    sr_res = extract_ats_board_from_url("https://jobs.smartrecruiters.com/LinkedIn3/999")
    assert sr_res == {
        "platform": "smartrecruiters",
        "slug": "LinkedIn3",
        "company": "LinkedIn3",
    }

    # Non-ATS
    assert extract_ats_board_from_url("https://google.com/search") is None


def test_register_discovered_board_and_load():
    mock_storage = []

    class MockCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params=None):
            self.last_query = query
            self.last_params = params
            if "INSERT INTO discovered_ats_boards" in query:
                # params: (platform, slug, company, metadata_json)
                plat, slug, company, _meta = params
                if any(x["platform"] == plat and x["slug"] == slug for x in mock_storage):
                    self.result = []
                else:
                    mock_storage.append({
                        "platform": plat,
                        "slug": slug,
                        "company": company,
                        "metadata": {"tenant": "testco", "wd_instance": "wd1", "site": "Careers"},
                    })
                    self.result = [(1,)]
            elif "SELECT platform, slug" in query:
                self.result = mock_storage

        def fetchone(self):
            return self.result[0] if self.result else None

        def fetchall(self):
            return self.result

    class MockConn:
        def cursor(self):
            return MockCursor()

        def commit(self):
            pass

    test_url = "https://testco.wd1.myworkdayjobs.com/en-US/Careers/job/Bangalore/ML_1"
    conn = MockConn()
    inserted = register_discovered_board(test_url, conn=conn)
    assert inserted is True

    # Duplicate should not re-insert
    inserted_again = register_discovered_board(test_url, conn=conn)
    assert inserted_again is False

    loaded = load_discovered_boards(conn=conn)
    assert any(
        w.get("tenant") == "testco" and w.get("site") == "Careers"
        for w in loaded["workday_cxs"]
    )


@pytest.mark.asyncio
async def test_resolve_direct_apply_url():
    # Direct ATS URL should return immediately
    direct_url = "https://boards.greenhouse.io/sample/jobs/123"
    async with httpx.AsyncClient() as client:
        resolved = await resolve_direct_apply_url(client, "Sample", "ML Engineer", direct_url)
        assert resolved == direct_url

    # Non-ATS URL searches DDG HTML
    fallback = "https://www.linkedin.com/jobs/view/999999"
    ddg_mock_html = """
    <html>
      <body>
        <div class="results">
          <a class="result__url" href="https://jobs.lever.co/sampleco/12345">Sample Lever Link</a>
        </div>
      </body>
    </html>
    """
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post("https://html.duckduckgo.com/html/").respond(
                status_code=200, html=ddg_mock_html
            )
            resolved = await resolve_direct_apply_url(client, "SampleCo", "AI Engineer", fallback)
            assert resolved == "https://jobs.lever.co/sampleco/12345"


@pytest.mark.asyncio
async def test_poll_jobboards_flow(monkeypatch):
    mock_df = pd.DataFrame(
        [
            {
                "title": "Machine Learning Engineer",
                "company": "InnovateAI",
                "location": "Hyderabad, India",
                "description": "<p>Deep learning and Python experience required.</p>",
                "job_url": "https://www.linkedin.com/jobs/view/112233",
                "job_url_direct": "https://jobs.ashbyhq.com/innovateai/job-1",
                "site": "linkedin",
            },
            {
                "title": "Chef de Cuisine",
                "company": "Hotel Grand",
                "location": "Paris, France",
                "description": "Culinary arts",
                "job_url": "https://hotel.com/job/2",
                "job_url_direct": "",
                "site": "indeed",
            },
        ]
    )

    def mock_scrape_sync(**kwargs):
        return mock_df

    registered_urls = []
    inserted_jobs = []

    monkeypatch.setattr("src.ingestors.jobboards_poller._scrape_sync", mock_scrape_sync)
    monkeypatch.setattr(
        "src.ingestors.jobboards_poller.register_discovered_board",
        lambda url: registered_urls.append(url),
    )
    monkeypatch.setattr(
        "src.ingestors.jobboards_poller.insert_job",
        lambda *args, **kwargs: inserted_jobs.append(args) or True,
    )

    added = await poll_jobboards(
        search_terms=["Machine Learning Engineer"],
        locations=["Hyderabad, India"],
        sites=["linkedin"],
    )

    assert added == 1
    assert "https://jobs.ashbyhq.com/innovateai/job-1" in registered_urls
    assert len(inserted_jobs) == 1
    assert inserted_jobs[0][1] == "jobspy_linkedin"
    assert inserted_jobs[0][2] == "InnovateAI"


def test_get_jobboards_parameters_from_config_and_profile(monkeypatch):
    terms, locs, target_sites, res_count, hrs = get_jobboards_parameters()

    # Verify parameters are sourced from config/profile
    assert len(terms) > 0
    assert any("Machine Learning" in t or "AI" in t for t in terms)
    assert len(locs) > 0
    assert any("India" in l or "Hyderabad" in l for l in locs)
    assert "naukri" in target_sites
    assert "linkedin" in target_sites
    assert res_count == 20
    assert hrs == 72

    # Verify custom overrides take priority
    custom_terms, custom_locs, custom_sites, c_count, c_hrs = get_jobboards_parameters(
        search_terms=["Robotics Engineer"],
        locations=["Tokyo, Japan"],
        sites=["indeed"],
        results_wanted=5,
        hours_old=24,
    )
    assert custom_terms == ["Robotics Engineer"]
    assert custom_locs == ["Tokyo, Japan"]
    assert custom_sites == ["indeed"]
    assert c_count == 5
    assert c_hrs == 24

