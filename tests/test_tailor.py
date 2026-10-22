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