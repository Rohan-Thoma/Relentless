from src.tailor.ats_scorer import compute_ats_scorecard, format_scorecard_markdown


def test_compute_ats_scorecard_metrics():
    jd_text = """
    We are seeking a Senior Machine Learning Engineer with deep expertise in PyTorch,
    LangGraph, Vector Databases, Kubernetes, and Go.
    Responsibilities:
    - Optimize inference latency and throughput for LLMs
    - Build automated data pipelines and edge inference with TensorRT
    - Develop microservices with FastAPI and Docker
    """

    raw_profile = {
        "core_skills": [
            "Python", "PyTorch", "TensorRT", "FastAPI", "Docker", "SQL"
        ],
        "experience_summary": "Machine Learning Engineer with experience in PyTorch and Docker.",
        "experience": [
            {
                "role": "ML Engineer",
                "bullets": [
                    "Built machine learning models using PyTorch and FastAPI.",
                    "Deployed containerized services with Docker.",
                ],
            }
        ],
    }

    tailored_output = {
        "tailored_summary": "Staff-level Machine Learning Engineer specializing in LangGraph, PyTorch, and TensorRT inference latency optimization.",
        "matched_keywords": ["PyTorch", "LangGraph", "TensorRT", "FastAPI", "Latency", "Docker"],
        "skill_categories": [
            {
                "category": "GenAI & LLMs",
                "skills": ["LangGraph", "Vector Databases", "Prompt Engineering"],
            },
            {
                "category": "ML & Edge Inference",
                "skills": ["PyTorch", "TensorRT", "Latency Profiling"],
            },
        ],
        "tailored_bullets_role_1": [
            "Architected low-latency inference pipelines with TensorRT and PyTorch, achieving 45 FPS and 35% latency reduction.",
            "Engineered distributed microservices using FastAPI and Docker, handling 15M+ requests per day.",
            "Implemented vector databases with LangGraph for hybrid semantic retrieval.",
        ],
        "tailored_bullets_role_2": [],
        "tailored_bullets_role_3": [],
        "change_rationale": ["Emphasized LangGraph and latency optimization"],
    }

    scorecard = compute_ats_scorecard(jd_text, raw_profile, tailored_output)

    # Assertions
    assert scorecard["overall_after_pct"] > scorecard["overall_before_pct"]
    assert scorecard["boost_pct"] > 0
    assert scorecard["hard_skills_after_pct"] >= scorecard["hard_skills_before_pct"]
    assert scorecard["impact_density_pct"] >= 80
    assert scorecard["total_jd_skills"] > 0

    # Verify newly added terms captured (e.g. LangGraph / Vector Databases)
    assert any("LangGraph" in term or "Vector" in term for term in scorecard["newly_added_terms"])

    # Verify missing JD skills detected (Kubernetes / Go not in profile or tailored)
    assert any("Kubernetes" in m or "Go" in m for m in scorecard["missing_jd_skills"])


def test_format_scorecard_markdown():
    mock_scorecard = {
        "overall_before_pct": 64,
        "overall_after_pct": 91,
        "boost_pct": 27,
        "hard_skills_before_pct": 60,
        "hard_skills_after_pct": 92,
        "total_jd_skills": 12,
        "matched_skills_count": 11,
        "domain_keywords_before_pct": 65,
        "domain_keywords_after_pct": 88,
        "impact_density_pct": 95,
        "newly_added_terms": ["LangSmith", "Hybrid Retrieval", "Latency Profiling"],
        "missing_jd_skills": ["Kubernetes", "Go"],
    }

    formatted = format_scorecard_markdown(mock_scorecard)

    assert "📊 Mock ATS Match Scorecard:" in formatted
    assert "64% (Raw) ➔ 91% (Tailored) [+27% Boost]" in formatted
    assert "92% (11/12 JD skills)" in formatted
    assert "Domain Keywords: 88% | Quantified Impact: 95%" in formatted
    assert "LangSmith, Hybrid Retrieval, Latency Profiling" in formatted
    assert "Kubernetes, Go (Reply to add if you know them!)" in formatted

