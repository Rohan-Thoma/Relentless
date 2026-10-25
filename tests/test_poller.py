import asyncio
import json
import pathlib

import httpx
import pytest
import respx

from src.ingestors.ats_poller import (
    clean_html,
    matches_basic_filters,
    poll_ashby,
    poll_greenhouse,
    poll_lever,
    poll_workday,
)

FIX = pathlib.Path(__file__).parent / "fixtures"


def test_matches_basic_filters():
    assert matches_basic_filters("AI Engineer", "Remote") is True
    assert matches_basic_filters("iOS Developer", "Remote") is False


def test_clean_html():
    assert clean_html("<p>Hello</p>") == "Hello"


@pytest.mark.asyncio
async def test_poll_greenhouse_flow(monkeypatch):
    monkeypatch.setattr("src.ingestors.ats_poller.insert_job", lambda *a, **k: True)
    monkeypatch.setattr("src.ingestors.ats_poller.job_exists", lambda *a, **k: False)
    data = json.loads((FIX / "greenhouse.json").read_text())
    with respx.mock:
        respx.get("https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true").mock(
            return_value=httpx.Response(200, json=data)
        )
        async with httpx.AsyncClient() as client:
            # poll_greenhouse needs conn; monkeypatch insert_job ignores conn
            n = await poll_greenhouse(client, None, "stripe")
    assert n == 1


@pytest.mark.asyncio
async def test_poll_lever_flow(monkeypatch):
    monkeypatch.setattr("src.ingestors.ats_poller.insert_job", lambda *a, **k: True)
    monkeypatch.setattr("src.ingestors.ats_poller.job_exists", lambda *a, **k: False)
    data = json.loads((FIX / "lever.json").read_text())
    with respx.mock:
        respx.get("https://api.lever.co/v0/postings/zeta?mode=json").mock(
            return_value=httpx.Response(200, json=data)
        )
        async with httpx.AsyncClient() as client:
            n = await poll_lever(client, None, "zeta")
    assert n == 1


@pytest.mark.asyncio
async def test_poll_ashby_flow(monkeypatch):
    monkeypatch.setattr("src.ingestors.ats_poller.insert_job", lambda *a, **k: True)
    monkeypatch.setattr("src.ingestors.ats_poller.job_exists", lambda *a, **k: False)
    data = json.loads((FIX / "ashby.json").read_text())
    with respx.mock:
        respx.get("https://api.ashbyhq.com/posting-api/job-board/supabase").mock(
            return_value=httpx.Response(200, json=data)
        )
        async with httpx.AsyncClient() as client:
            n = await poll_ashby(client, None, "supabase")
    assert n == 1


@pytest.mark.asyncio
async def test_poll_workday_flow(monkeypatch):
    monkeypatch.setattr("src.ingestors.ats_poller.insert_job", lambda *a, **k: True)
    monkeypatch.setattr("src.ingestors.ats_poller.job_exists", lambda *a, **k: False)
    monkeypatch.setattr("src.ingestors.ats_poller.is_near_dup", lambda *a, **k: False)
    list_data = json.loads((FIX / "workday_list.json").read_text())
    detail_data = json.loads((FIX / "workday_detail.json").read_text())
    cfg = {"company": "NVIDIA", "tenant": "nvidia", "wd_instance": "wd5", "site": "NVIDIAExternalCareerSite"}
    with respx.mock:
        respx.post("https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs").mock(
            return_value=httpx.Response(200, json=list_data)
        )
        respx.get("https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/job/1").mock(
            return_value=httpx.Response(200, json=detail_data)
        )
        async with httpx.AsyncClient() as client:
            n = await poll_workday(client, None, cfg)
    assert n == 1


@pytest.mark.asyncio
async def test_run_ingestion_execution_order_and_modes(monkeypatch):
    from src.config import AppConfig, IngestionConfig
    from src.ingestors.ats_poller import run_ingestion

    test_cfg = AppConfig(ingestion=IngestionConfig(prioritize_scraping=True, scrape_only=False, enable_ats=True))
    monkeypatch.setattr("src.ingestors.ats_poller._cfg", lambda: test_cfg)

    call_order = []

    async def mock_scraping(sources=None):
        call_order.append("scraping")
        return 3

    async def mock_ats(platforms=None):
        call_order.append("ats")
        return 2

    monkeypatch.setattr("src.ingestors.ats_poller.poll_all_scraping", mock_scraping)
    monkeypatch.setattr("src.ingestors.ats_poller.poll_all_ats", mock_ats)

    # 1. Default: Scraping first, then ATS
    call_order.clear()
    total = await run_ingestion(prioritize_scraping=True)
    assert total == 5
    assert call_order == ["scraping", "ats"], "Scraping must run BEFORE ATS by default!"

    # 2. Scrape only: ATS must be skipped
    call_order.clear()
    total = await run_ingestion(scrape_only=True)
    assert total == 3
    assert call_order == ["scraping"], "ATS should not run when scrape_only=True"

    # 3. ATS only: Scraping must be skipped
    call_order.clear()
    total = await run_ingestion(ats_only=True)
    assert total == 2
    assert call_order == ["ats"], "Scraping should not run when ats_only=True"


@pytest.mark.asyncio
async def test_poll_all_scraping_sources_selective(monkeypatch):
    from src.ingestors.ats_poller import poll_all_scraping

    called = []

    async def mock_jb():
        called.append("jobboards")
        return 4

    async def mock_gmail():
        called.append("gmail")
        return 2

    monkeypatch.setattr("src.ingestors.ats_poller.poll_jobboards", mock_jb)
    monkeypatch.setattr("src.ingestors.ats_poller.poll_gmail_alerts", mock_gmail)

    # Jobboards only
    called.clear()
    res = await poll_all_scraping(sources=["jobboards"])
    assert res == 4
    assert called == ["jobboards"]

    # Gmail only
    called.clear()
    res = await poll_all_scraping(sources=["gmail"])
    assert res == 2
    assert called == ["gmail"]

    # Both
    called.clear()
    res = await poll_all_scraping(sources=["jobboards", "gmail"])
    assert res == 6
    assert called == ["jobboards", "gmail"]


def test_manage_py_flag_parser():
    import manage

    flags = manage._parse_run_flags(["--scrape-only", "--once"])
    assert flags["scrape_only"] is True
    assert flags["skip_ats"] is True
    assert flags["once"] is True

    flags = manage._parse_run_flags(["--jobboards-only"])
    assert flags["sources"] == ["jobboards"]
    assert flags["scrape_only"] is True
    assert flags["skip_ats"] is True

    flags = manage._parse_run_flags(["--sources=jobboards,gmail", "--fast"])
    assert flags["sources"] == ["jobboards", "gmail"]
    assert flags["scrape_only"] is True


@pytest.mark.asyncio
async def test_main_loop_scrape_first_and_once(monkeypatch):
    import src.main_loop as ml

    call_seq = []

    async def mock_scrape(sources=None):
        call_seq.append("scrape")
        return 1

    async def mock_ats(platforms=None):
        call_seq.append("ats")
        return 0

    from src.config import AppConfig, IngestionConfig
    test_cfg = AppConfig(ingestion=IngestionConfig(prioritize_scraping=True, scrape_only=False, enable_ats=True))
    monkeypatch.setattr(ml, "load_config", lambda: test_cfg)
    monkeypatch.setattr(ml, "poll_all_scraping", mock_scrape)
    monkeypatch.setattr(ml, "poll_all_ats", mock_ats)
    monkeypatch.setattr(ml, "_process_digest_and_triage", lambda *a, **k: asyncio.sleep(0.001))
    monkeypatch.setattr(ml, "run_distill", lambda: {"concepts": 0})
    monkeypatch.setattr(ml, "init_db", lambda: None)
    monkeypatch.setattr(ml, "hydrate", lambda: None)

    # Scrape only with once=True
    await ml.main(scrape_only=True, once=True)
    assert call_seq == ["scrape"], "Should only scrape and terminate loop"

    # Default order with once=True
    call_seq.clear()
    await ml.main(scrape_only=False, once=True)
    assert call_seq == ["scrape", "ats"], "Should scrape first, then ats, then exit"
