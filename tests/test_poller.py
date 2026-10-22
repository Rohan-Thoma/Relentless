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