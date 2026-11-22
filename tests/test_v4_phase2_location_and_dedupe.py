"""Phase 2 tests: Exhaustive LocationPolicy (remote-only, city+remote, hybrid-only, onsite-only, AND/OR/XOR,
US/EU remote rejection, Indiana vs India, JD geo blockers, JobSpy is_remote, and scoped dedupe).
"""

import pandas as pd

from src.config import AppConfig, CandidatePreferences, LocationPolicy, LocationRule
from src.dedupe import normalize_title
from src.ingestors.common import extract_min_required_years, matches_basic_filters
from src.ingestors.jobboards_poller import poll_jobboards
from src.ingestors.location_policy import (
    derive_jobboard_queries_from_policy,
    evaluate_job_location,
    extract_job_location_facts,
    normalize_location_text,
)


def _cfg_with_policy(policy: LocationPolicy, max_exp: int | None = None) -> AppConfig:
    return AppConfig(
        candidate_preferences=CandidatePreferences(
            target_role_keywords=["machine learning", "ai engineer", "python", "mlops", "llm", "computer vision", "backend"],
            exclude_title_keywords=["intern", "internship", "staff", "principal", "director", "manager", "ios", "android", "frontend", "lead"],
            max_experience_years=max_exp,
        ),
        location_policy=policy,
    )


def test_indiana_never_becomes_india():
    assert ", india" not in normalize_location_text("Indianapolis, IN")
    assert normalize_location_text("Indianapolis, IN") == "indianapolis, in"
    assert ", india" not in normalize_location_text("Bloomington, IN")
    assert ", india" not in normalize_location_text("Fort Wayne, IN, USA")
    # Indian cities with ', IN' STILL resolve properly to India!
    assert "india" in normalize_location_text("Pune, IN")
    assert "india" in normalize_location_text("Hyderabad, IN")
    assert "india" in normalize_location_text("Bengaluru, IN")

    facts_in = extract_job_location_facts("AI Engineer", "Indianapolis, IN")
    assert "us" in facts_in.countries
    assert "india" not in facts_in.countries

    facts_pune = extract_job_location_facts("AI Engineer", "Pune, IN")
    assert "india" in facts_pune.countries
    assert "pune" in facts_pune.cities


def test_strict_remote_only_policy_rejects_hybrid_onsite_and_geo_restricted_us_eu():
    remote_only = LocationPolicy(
        operator="OR",
        candidate_country="india",
        reject_countries=["us", "canada", "uk", "eu", "emea", "europe", "australia", "latam"],
        rules=[
            LocationRule(
                name="strict_remote",
                work_modes=["remote"],
                allowed_regions=["anywhere", "worldwide", "global", "india", "apac"],
                allow_unspecified_remote=True,
            )
        ],
    )
    cfg = _cfg_with_policy(remote_only)

    # MUST ACCEPT: Genuine remote (worldwide, global, India, APAC, bare Remote)
    assert matches_basic_filters("AI Engineer", "Remote", cfg=cfg) is True
    assert matches_basic_filters("AI Engineer", "Remote - Worldwide", cfg=cfg) is True
    assert matches_basic_filters("AI Engineer", "Anywhere", cfg=cfg) is True
    assert matches_basic_filters("AI Engineer", "Remote, India", cfg=cfg) is True
    assert matches_basic_filters("AI Engineer", "Hyderabad, India (Remote)", cfg=cfg) is True
    assert matches_basic_filters("AI Engineer", "Remote - APAC", cfg=cfg) is True
    assert matches_basic_filters("AI Engineer", "Work From Home", cfg=cfg) is True

    # MUST REJECT: Hybrid & On-site (even in Hyderabad/Bengaluru!)
    assert matches_basic_filters("AI Engineer", "Hyderabad, India", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "Bengaluru, India (Hybrid)", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "San Francisco, CA (Hybrid)", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "London, UK - Hybrid", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "Tokyo, APAC", cfg=cfg) is False

    # MUST REJECT: Geo-restricted US/EU/UK/Canada remote & Federal/Public Sector
    assert matches_basic_filters("AI Engineer", "San Francisco, CA or Remote, US", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "Remote - United States", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "Remote (North America)", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "Remote - EMEA", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "Remote - UK", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "Remote - Canada", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "Maryland; Virginia; Washington, D.C.", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "Indianapolis, IN", cfg=cfg) is False

    # MUST REJECT: JD work-auth / citizenship / 5-day-in-office blockers
    assert (
        matches_basic_filters(
            "AI Engineer",
            "Remote",
            jd_text="Candidates must be authorized to work in the United States without sponsorship.",
            cfg=cfg,
        )
        is False
    )
    assert (
        matches_basic_filters(
            "AI Engineer",
            "Remote",
            jd_text="This role requires 5 days a week in-office collaboration.",
            cfg=cfg,
        )
        is False
    )


def test_remote_plus_specific_city_policy():
    policy = LocationPolicy(
        operator="OR",
        candidate_country="india",
        reject_countries=["us", "canada", "uk", "eu", "emea"],
        rules=[
            LocationRule(
                name="remote_global_or_india",
                work_modes=["remote"],
                allowed_regions=["anywhere", "worldwide", "global", "india", "apac"],
            ),
            LocationRule(
                name="hyderabad_bangalore_office_or_hybrid",
                work_modes=["hybrid", "onsite"],
                cities=["hyderabad", "bengaluru"],
                countries=["india"],
            ),
        ],
    )
    cfg = _cfg_with_policy(policy)

    # Accepts remote + Hyderabad/Bengaluru office & hybrid
    assert matches_basic_filters("AI Engineer", "Remote", cfg=cfg) is True
    assert matches_basic_filters("AI Engineer", "Hyderabad, India", cfg=cfg) is True
    assert matches_basic_filters("AI Engineer", "Bengaluru, Karnataka (Hybrid)", cfg=cfg) is True
    assert matches_basic_filters("AI Engineer", "Secunderabad, Telangana", cfg=cfg) is True

    # Rejects other Indian cities when only Hyderabad/Bengaluru are configured
    assert matches_basic_filters("AI Engineer", "Mumbai, India", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "Chennai, India (Hybrid)", cfg=cfg) is False
    # Rejects US remote / US office
    assert matches_basic_filters("AI Engineer", "Remote, US", cfg=cfg) is False
    assert matches_basic_filters("AI Engineer", "Austin, TX", cfg=cfg) is False


def test_hybrid_only_and_onsite_only_and_not_exclusions():
    hybrid_only = LocationPolicy(
        operator="OR",
        candidate_country="india",
        rules=[
            LocationRule(
                name="hybrid_bengaluru_only",
                work_modes=["hybrid"],
                cities=["bengaluru"],
                countries=["india"],
            )
        ],
    )
    cfg_h = _cfg_with_policy(hybrid_only)
    assert matches_basic_filters("AI Engineer", "Bengaluru, India (Hybrid)", cfg=cfg_h) is True
    assert matches_basic_filters("AI Engineer", "Bengaluru, India", cfg=cfg_h) is False  # onsite rejected!
    assert matches_basic_filters("AI Engineer", "Remote, India", cfg=cfg_h) is False  # remote rejected!

    # India onsite/hybrid EXCEPT Mumbai (testing not_cities)
    india_no_mumbai = LocationPolicy(
        operator="OR",
        candidate_country="india",
        rules=[
            LocationRule(
                name="india_except_mumbai",
                work_modes=["hybrid", "onsite"],
                countries=["india"],
                not_cities=["mumbai"],
            )
        ],
    )
    cfg_nm = _cfg_with_policy(india_no_mumbai)
    assert matches_basic_filters("AI Engineer", "Pune, India", cfg=cfg_nm) is True
    assert matches_basic_filters("AI Engineer", "Mumbai, India", cfg=cfg_nm) is False


def test_boolean_and_xor_combinators():
    # AND: Must be BOTH remote AND explicitly India
    and_policy = LocationPolicy(
        operator="AND",
        candidate_country="india",
        rules=[
            LocationRule(name="must_be_remote", work_modes=["remote"]),
            LocationRule(name="must_be_india", countries=["india"]),
        ],
    )
    ok1, _, _ = evaluate_job_location("AI Engineer", "Remote, India", policy=and_policy)
    ok2, _, _ = evaluate_job_location("AI Engineer", "Remote, Worldwide", policy=and_policy)
    ok3, _, _ = evaluate_job_location("AI Engineer", "Hyderabad, India", policy=and_policy)
    assert ok1 is True
    assert ok2 is False
    assert ok3 is False

    # XOR: Exactly one of (Remote) XOR (Hyderabad)
    xor_policy = LocationPolicy(
        operator="XOR",
        candidate_country="india",
        rules=[
            LocationRule(name="is_remote", work_modes=["remote"]),
            LocationRule(name="is_hyderabad", cities=["hyderabad"]),
        ],
    )
    # Remote only -> 1 match -> True
    assert evaluate_job_location("AI Engineer", "Remote, Worldwide", policy=xor_policy)[0] is True
    # Hyderabad onsite -> 1 match -> True
    assert evaluate_job_location("AI Engineer", "Hyderabad, India", policy=xor_policy)[0] is True
    # Both Remote AND Hyderabad -> 2 matches -> False under XOR!
    assert evaluate_job_location("AI Engineer", "Hyderabad, India (Remote)", policy=xor_policy)[0] is False


def test_intern_and_experience_years_filtering():
    cfg = _cfg_with_policy(LocationPolicy(), max_exp=6)
    assert matches_basic_filters("Machine Learning Intern", "Remote", cfg=cfg) is False
    assert matches_basic_filters("AI Engineering Internship", "Remote", cfg=cfg) is False
    assert extract_min_required_years("AI Engineer (6 to 10 years)", "") == 6
    assert extract_min_required_years("AI Engineer", "Requires 8+ years of experience in ML") == 8
    # 8+ years exceeds max_experience_years=6 -> rejected
    assert matches_basic_filters("AI Engineer", "Remote", jd_text="Minimum 8+ years of experience required.", cfg=cfg) is False
    # 3-7 years has min=3 <= 6 -> accepted!
    assert matches_basic_filters("AI Engineer", "Remote", jd_text="Requires 3-7 years of experience.", cfg=cfg) is True


def test_dedupe_preserves_seniority_and_work_mode():
    assert normalize_title("Senior Machine Learning Engineer") != normalize_title("Junior Machine Learning Engineer")
    assert normalize_title("ML Engineer Remote") != normalize_title("ML Engineer Hybrid")
    assert normalize_title("Software Engineer II") != normalize_title("Software Engineer I")


def test_derive_jobboard_queries_and_jobspy_is_remote_flag(monkeypatch):
    remote_only_policy = LocationPolicy()
    queries = derive_jobboard_queries_from_policy(remote_only_policy)
    assert any(q["location"] == "Remote" and q["is_remote"] is True for q in queries)

    captured_calls = []

    def mock_scrape_sync(**kwargs):
        captured_calls.append(kwargs)
        return pd.DataFrame(
            [
                {
                    "id": "li-remote-1",
                    "title": "Machine Learning Engineer",
                    "company": "GlobalAI",
                    "location": "Bengaluru, India",
                    "is_remote": True,
                    "description": "<p>Python PyTorch remote role</p>",
                    "job_url": "https://www.linkedin.com/jobs/view/9911",
                    "job_url_direct": "https://boards.greenhouse.io/globalai/jobs/1",
                    "site": "linkedin",
                },
                {
                    "id": "li-onsite-2",
                    "title": "Machine Learning Engineer",
                    "company": "OfficeCorp",
                    "location": "Bengaluru, India",
                    "is_remote": False,
                    "description": "<p>Onsite Bengaluru role</p>",
                    "job_url": "https://www.linkedin.com/jobs/view/9922",
                    "job_url_direct": "",
                    "site": "linkedin",
                },
            ]
        )

    inserted = []
    monkeypatch.setattr("src.ingestors.jobboards_poller._scrape_sync", mock_scrape_sync)
    monkeypatch.setattr("src.ingestors.jobboards_poller.register_discovered_board", lambda url: False)
    monkeypatch.setattr(
        "src.ingestors.jobboards_poller.insert_job",
        lambda *args, **kwargs: inserted.append((args, kwargs)) or True,
    )

    import asyncio

    added = asyncio.run(
        poll_jobboards(search_terms=["Machine Learning Engineer"], locations=["Remote"], sites=["linkedin"])
    )
    assert captured_calls[0]["is_remote"] is True
    # Only the is_remote=True job passes under default remote-only policy!
    assert added == 1
    assert inserted[0][0][2] == "GlobalAI"
    assert inserted[0][1]["work_mode"] == "remote"
