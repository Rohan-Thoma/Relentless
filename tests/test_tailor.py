from src.tailor.resume_tailor import TailoredResumeOutput, escape_typst


def test_escape_typst_special_chars():
    for ch in ["\\", "#", "$", "@", "*", "_", "<", ">", "`"]:
        assert "\\" + ch in escape_typst(ch)


def test_tailored_resume_output_validation():
    data = {
        "tailored_summary": "s",
        "matched_keywords": ["a"],
        "tailored_bullets_role_1": ["b1", "b2", "b3"],
        "tailored_bullets_role_2": ["c1", "c2"],
        "change_rationale": ["r1"],
    }
    obj = TailoredResumeOutput(**data)
    assert obj.tailored_summary == "s"


def test_tailored_resume_output_normalizes_dict_and_string_variations():
    """Verifies that dict-based skill_categories and str-based change_rationale normalize gracefully."""
    data = {
        "tailored_summary": ["Experienced AI Engineer", "Built LLM solutions."],
        "matched_keywords": "Python, PyTorch, Docker, LangChain",
        "skill_categories": {
            "GenAI & Agentic Systems": ["LangChain", "LlamaIndex"],
            "MLOps": "MLflow, DVC",
        },
        "tailored_bullets_role_1": "• Scaled agent pipeline\n• Reduced inference latency by 40%",
        "tailored_bullets_role_2": ["Fine-tuned open models."],
        "tailored_bullets_role_3": ["Wrote baseline models."],
        "change_rationale": "The tailored resume emphasizes GenAI workflows for Epam Systems.",
    }
    obj = TailoredResumeOutput(**data)
    assert "Experienced AI Engineer" in obj.tailored_summary
    assert obj.matched_keywords == ["Python", "PyTorch", "Docker", "LangChain"]
    assert len(obj.skill_categories) == 2
    assert obj.skill_categories[0].category == "GenAI & Agentic Systems"
    assert obj.skill_categories[0].skills == ["LangChain", "LlamaIndex"]
    assert obj.skill_categories[1].category == "MLOps"
    assert obj.skill_categories[1].skills == ["MLflow", "DVC"]
    assert len(obj.tailored_bullets_role_1) == 2
    assert obj.change_rationale == ["The tailored resume emphasizes GenAI workflows for Epam Systems."]
