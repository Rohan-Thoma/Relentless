"""Phase 4 tests: Honest un-clamped ATS scorecard, word-boundary JD parsing,
LangGraph TailorGraph cyclic preservation/refinement loop, 3-tier skill transparency,
fail-loud Jev audit, LangGraph TriageGraph (full profile + no calibration death spiral),
and Intelligent Skip-Reason Learning Gate.
"""

import pytest

from src.calibrate import CalibrationModel
from src.jd_parse import parse_jd
from src.knowledge.distiller import should_learn_from_skip
from src.orchestrator.graph import run_tailor_graph, run_triage_graph
from src.tailor.ats_scorer import compute_ats_scorecard
from src.tailor.resume_tailor import AuditUnavailable, classify_tailored_skills


def test_jd_parse_uses_strict_word_boundaries():
    # Previously 'go' matched 'good', 'rag' matched 'storage', 'rest' matched 'interested'!
    jd = "We are interested in good storage management algorithms and strong communication."
    parsed = parse_jd(jd)
    skills_low = {s.lower() for s in parsed["skills"]}
    assert "go" not in skills_low
    assert "rag" not in skills_low
    assert "rest" not in skills_low

    jd2 = "Seeking Python, Go, RAG, and REST APIs experience."
    parsed2 = parse_jd(jd2)
    skills2 = {s.lower() for s in parsed2["skills"]}
    assert {"python", "go", "rag", "rest"}.issubset(skills2)


def test_honest_ats_scorecard_reports_negative_regression_without_fake_boost():
    jd = "Seeking a Machine Learning Engineer with Python, PyTorch, FastAPI, Docker, SQL, and latency optimization."
    profile = {
        "core_skills": ["Python", "PyTorch", "FastAPI", "Docker", "SQL"],
        "experience_summary": "ML Engineer building low-latency Python, PyTorch, and FastAPI systems.",
        "experience": [
            {
                "company": "TechCo",
                "role": "ML Engineer",
                "dates": "2024 - Present",
                "bullets": [
                    "Architected Python and PyTorch inference pipelines achieving 40% latency reduction at 20 FPS.",
                    "Deployed FastAPI and Docker microservices backed by SQL handling 10M+ requests.",
                ],
            }
        ],
    }

    # Badly tailored output that DROPPED Python, PyTorch, Docker, metrics, and stuffed unrendered matched_keywords!
    bad_tailored = {
        "tailored_summary": "Engineer working on software tasks.",
        "matched_keywords": ["Python", "PyTorch", "FastAPI", "Docker", "SQL"],  # NOT rendered when skill_categories exists!
        "skill_categories": [{"category": "General", "skills": ["Communication", "Teamwork"]}],
        "tailored_bullets_by_role": [["Helped with general software tasks."]],
    }
    sc = compute_ats_scorecard(jd, profile, bad_tailored)

    # MUST report an HONEST negative delta (zero +5% floor, zero max(before,after) clamping!)
    assert sc["hard_skills_after_pct"] < sc["hard_skills_before_pct"]
    assert sc["overall_after_pct"] < sc["overall_before_pct"]
    assert sc["boost_pct"] < 0
    assert "Python" in sc["dropped_jd_skills"]
    assert "PyTorch" in sc["dropped_jd_skills"]


def test_three_tier_skill_transparency_and_user_sovereignty():
    profile = {
        "core_skills": ["Python", "Pandas", "FastAPI", "Docker", "PyTorch"],
        "experience": [{"company": "A", "role": "MLE", "bullets": ["Built FastAPI services with Docker and Pandas."]}],
    }
    tailored = {
        "skill_categories": [
            {
                "category": "Stack",
                "skills": ["Python", "NumPy", "REST APIs", "Kubernetes"],
            }
        ]
    }
    # Without user instruction: Python=verified, NumPy & REST APIs=adjacent_bridged, Kubernetes=unverified_stretch!
    tiers = classify_tailored_skills(profile, tailored, custom_instruction="")
    assert "Python" in tiers["verified"]
    assert "NumPy" in tiers["adjacent_bridged"]
    assert "REST APIs" in tiers["adjacent_bridged"]
    assert "Kubernetes" in tiers["unverified_stretch"]

    # WITH explicit user instruction ("add Kubernetes"): user is sovereign -> treated as verified!
    tiers_sovereign = classify_tailored_skills(profile, tailored, custom_instruction="Please add Kubernetes to my skills")
    assert "Kubernetes" in tiers_sovereign["verified"]
    assert "Kubernetes" not in tiers_sovereign["unverified_stretch"]


@pytest.mark.asyncio
async def test_langgraph_tailor_graph_refines_bad_draft_and_fails_loud_on_audit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    profile = {
        "name": "Ada Test",
        "email": "ada@example.com",
        "location": "Remote",
        "core_skills": ["Python", "PyTorch", "FastAPI", "Docker", "TensorRT", "SQL"],
        "experience_summary": "ML Engineer with 3+ years building PyTorch and FastAPI AI systems.",
        "experience": [
            {
                "company": "AlphaAI",
                "role": "ML Engineer",
                "Location": "Hyderabad, India",
                "dates": "2024 - Present",
                "bullets": [
                    "Built PyTorch OCR achieving 95% accuracy on KYC documents.",
                    "Deployed TensorRT models at 15 FPS using FastAPI and Docker.",
                    "Automated data pipelines reducing workload by 50x and saving 100+ hours.",
                ],
            }
        ],
        "education": [{"degree": "M.Tech", "institution": "IIT", "dates": "2020"}],
    }
    monkeypatch.setattr("src.orchestrator.graph.get_master_profile", lambda: profile)
    monkeypatch.setattr("src.tailor.resume_tailor.get_master_profile", lambda: profile)

    calls = []

    async def fake_call_llm(client, job, profile, custom_instruction="", okf_edit_habits="", refinement_feedback="", prior_draft=None):
        calls.append({"attempt": len(calls) + 1, "feedback": refinement_feedback})
        if len(calls) == 1:
            # First draft intentionally bad: cliché opener, drops Python, strips metrics, fabricates Kubernetes!
            return {
                "tailored_summary": "Results-driven engineer with experience in AI.",
                "matched_keywords": ["Python", "Kubernetes"],
                "skill_categories": [{"category": "MLOps", "skills": ["Docker", "Kubernetes"]}],
                "tailored_bullets_by_role": [["Worked on OCR and deployment pipelines."]],
                "change_rationale": ["Draft 1"],
            }
        # Second draft fixes all violations after LangGraph looped back with exact feedback!
        return {
            "tailored_summary": "Machine Learning Engineer experienced in production Python, PyTorch low-latency inference, TensorRT optimization, and FastAPI microservices.",
            "matched_keywords": ["Python", "PyTorch", "TensorRT", "FastAPI", "Docker", "Inference", "Latency"],
            "skill_categories": [
                {"category": "ML & Inference", "skills": ["Python", "PyTorch", "TensorRT", "Inference Optimization", "Latency"]},
                {"category": "Backend & MLOps", "skills": ["FastAPI", "REST APIs", "Docker", "SQL"]},
            ],
            "tailored_bullets_by_role": [
                [
                    "Engineered production Python and PyTorch OCR achieving 95% accuracy on KYC documents with low-latency inference.",
                    "Deployed TensorRT inference models at 15 FPS using FastAPI and Docker microservices.",
                    "Automated SQL data pipelines reducing manual workload by 50x and saving 100+ hours.",
                ]
            ],
            "change_rationale": ["Preserved all verified metrics and bridged REST APIs / Inference Optimization."],
        }

    async def fake_audit_unavailable(client, tailored, custom_instruction=""):
        raise AuditUnavailable("simulated outage")

    monkeypatch.setattr("src.orchestrator.graph.call_tailor_llm", fake_call_llm)
    monkeypatch.setattr("src.orchestrator.graph.verify_no_hallucination_with_jev", fake_audit_unavailable)

    job = {
        "id": 77,
        "company": "TargetAI",
        "title": "ML Engineer",
        "location": "Remote",
        "jev_persona": "ai_agent_engineer",
        "jd_text": "Seeking ML Engineer with Python, PyTorch, TensorRT, FastAPI, Docker, SQL, and low-latency inference.",
    }
    res = await run_tailor_graph(job)

    # Proves LangGraph ran the cyclic refine loop!
    assert len(calls) == 2
    assert "Python" in calls[1]["feedback"]
    assert "Kubernetes" in calls[1]["feedback"]
    assert res["refinement_passes"] == 2
    # Fail-loud audit: NEVER fakes 0.0% when Jev audit is unavailable!
    assert res["hallucination_prob"] is None
    assert res["audit_status"] == "UNAVAILABLE"
    assert "UNAVAILABLE" in res["diff_summary"]
    assert "REST APIs" in res["diff_summary"]  # Adjacent bridge transparently listed!


@pytest.mark.asyncio
async def test_langgraph_triage_graph_prevents_calibration_death_spiral(monkeypatch):
    profile = {
        "name": "Test Eng",
        "location": "Hyderabad, India",
        "core_skills": ["Python", "PyTorch", "LangGraph"],
        "experience_summary": "3.5 years ML Engineering",
        "experience": [{"company": "Co", "role": "MLE", "dates": "2023-2026", "bullets": ["Built RAG systems"]}],
        "application_defaults": {"total_experience_years": "3.5"},
    }
    monkeypatch.setattr("src.orchestrator.graph.get_master_profile", lambda: profile)
    monkeypatch.setattr("src.triage.jev_judge.get_master_profile", lambda: profile)

    # Simulate a very selective user where PAVA calibrated probability is squashed to 0.25!
    low_cal = CalibrationModel(mapping=[(0.0, 0.10), (0.85, 0.25)], n=40)

    async def fake_cal():
        return low_cal

    async def fake_jev(client, job, learned_context=""):
        return {
            "jev_scam_prob": 0.05,
            "jev_fit_tier": "dream_role",
            "jev_fit_prob": 0.88,
            "jev_persona": "ai_agent_engineer",
        }

    monkeypatch.setattr("src.orchestrator.graph.get_cached_calibration_model", fake_cal)
    monkeypatch.setattr("src.orchestrator.graph.evaluate_with_jev", fake_jev)

    enriched = await run_triage_graph(
        {"id": 501, "company": "GreatAI", "title": "AI Engineer", "location": "Remote", "jd_text": "Python PyTorch LangGraph"}
    )
    # Must STILL PASS because raw_fit (0.88) >= config min_fit_prob (0.65), while cal_fit (0.25) is saved for ranking!
    assert enriched["triage_passed"] is True
    assert enriched["jev_fit_prob"] == 0.88
    assert enriched["jev_fit_prob_cal"] == 0.25
    assert enriched["embedding_sim"] >= 0.0


def test_intelligent_skip_reason_learning_gate():
    # Expired job -> DO NOT LEARN!
    learn, _ = should_learn_from_skip("SKIP_BAD_FIT", "EXPIRED_OR_CLOSED", "Link returned 404")
    assert learn is False

    # Busy without durable signal -> DO NOT LEARN!
    learn_busy, _ = should_learn_from_skip("SKIP_BAD_FIT", "BUSY_OR_OTHER", "too busy today, maybe later")
    assert learn_busy is False

    # Busy/Other WITH durable preference note -> LEARN!
    learn_note, _ = should_learn_from_skip("SKIP_BAD_FIT", "BUSY_OR_OTHER", "Too senior, asks for 10 years Java experience")
    assert learn_note is True

    # Experience mismatch (e.g. intern) -> LEARN!
    learn_exp, _ = should_learn_from_skip("SKIP_BAD_FIT", "BAD_MATCH_EXPERIENCE", "This is an internship role")
    assert learn_exp is True

    # Scam -> LEARN!
    learn_scam, _ = should_learn_from_skip("SKIP_SCAM", "SCAM_OR_BAD_COMPANY", "Unpaid take-home trap")
    assert learn_scam is True


@pytest.mark.asyncio
async def test_triage_persist_rolls_back_before_fallback_and_uses_typed_sql(monkeypatch):
    """Regression test for PostgreSQL 'current transaction is aborted' bug in _triage_persist_node."""
    from contextlib import contextmanager

    from src.orchestrator.graph import _triage_persist_node

    monkeypatch.setenv("RELENTLESS_TEST_LIVE_DB", "1")

    state_flags = {"aborted": False, "rollbacks": 0, "committed": False, "sqls": []}

    class StrictPgCursor:
        def execute(self, sql, params=None):
            if state_flags["aborted"]:
                raise RuntimeError("current transaction is aborted, commands ignored until end of transaction block")
            state_flags["sqls"].append((sql, params))
            assert "CASE WHEN %s IS NOT NULL" not in sql, "Untyped SQL parameter triggers PostgreSQL IndeterminateDatatype!"
            if "embedding_model" in sql:
                # Simulate first query failing (e.g. missing vector extension or column)
                state_flags["aborted"] = True
                raise RuntimeError("simulated column error")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class StrictPgConn:
        def cursor(self):
            return StrictPgCursor()

        def rollback(self):
            state_flags["rollbacks"] += 1
            state_flags["aborted"] = False

        def commit(self):
            assert not state_flags["aborted"]
            state_flags["committed"] = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    @contextmanager
    def fake_conn():
        yield StrictPgConn()

    monkeypatch.setattr("src.orchestrator.graph.get_connection", fake_conn)

    out = await _triage_persist_node(
        {
            "job": {"id": 4001, "company": "First American", "title": "AI Engineer"},
            "cosine_sim": 0.42,
            "embedding": [0.01] * 1536,
            "embedding_model": "text-embedding-3-small",
            "embedding_sim": 0.77,
            "jev_result": {
                "jev_scam_prob": 0.05,
                "jev_fit_tier": "strong_fit",
                "jev_fit_prob": 0.85,
                "jev_persona": "ai_agent_engineer",
            },
            "jev_fit_prob_cal": 0.85,
            "triage_passed": True,
            "workflow_stage": "TRIAGED",
        }
    )

    assert state_flags["rollbacks"] == 1
    assert state_flags["committed"] is True
    assert len(state_flags["sqls"]) == 2
    assert out["enriched_job"]["triage_passed"] is True

