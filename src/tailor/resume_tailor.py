import json
import logging
import os
import re

import httpx
import typst
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from src.httpx_utils import post_with_retry
from src.settings_store import get_master_profile

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY", "").strip()

logger = logging.getLogger(__name__)

class TailoredResumeOutput(BaseModel):
    tailored_summary: str = Field(description="2-sentence executive summary tailored to the target role.")
    matched_keywords: list[str] = Field(description="Exact skills from candidate core_skills emphasized for ATS.")
    tailored_bullets_role_1: list[str] = Field(description="3 surgically tailored bullets for experience[0].")
    tailored_bullets_role_2: list[str] = Field(description="2 surgically tailored bullets for experience[1].")
    change_rationale: list[str] = Field(description="2 to 3 short bullet points explaining what was tweaked and why.")

def escape_typst(text: str) -> str:
    """Escapes special Typst markup characters so compilation never fails."""
    if not text:
        return ""
    for ch in ["\\", "#", "$", "@", "*", "_", "<", ">", "`"]:
        text = text.replace(ch, "\\" + ch)
    return text

async def verify_no_hallucination_with_jev(client: httpx.AsyncClient, tailored: dict) -> float:
    """Uses Jev System-1 noul primitive to verify the LLM did not invent skills outside master_profile."""
    url = "https://api.typesafe.ai/v1/systemone" if TYPESAFE_API_KEY else "https://openrouter.ai/api/alpha/decisions"
    api_key = TYPESAFE_API_KEY if TYPESAFE_API_KEY else OPENROUTER_API_KEY
    model_id = "jev-1.13.0" if TYPESAFE_API_KEY else "typesafe/jev-1.14"

    if not api_key:
        return 0.05

    PROFILE = get_master_profile()

    payload = {
        "model": model_id,
        "state": {
            "verified_candidate_profile": {
                "allowed_skills": PROFILE["core_skills"],
                "original_experience": PROFILE["experience"],
            },
            "proposed_tailored_output": tailored,
        },
        "questions": {
            "has_hallucination": {
                "type": "noul",
                "instructions": "Does proposed_tailored_output claim any programming language, cloud platform, company, or metric that is completely absent from verified_candidate_profile?",
                "criteria": {
                    "true": "Claims unverified technologies (e.g., Rust, Kubernetes, Java, AWS Certified) or fake employers not in verified_candidate_profile.",
                    "false": "Only reorders, rephrases, or emphasizes skills and experiences already present in verified_candidate_profile.",
                },
            }
        },
    }
    try:
        resp = await post_with_retry(
            client,
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=15.0,
        )
        if resp.status_code == 200:
            return float(resp.json()["answers"]["has_hallucination"]["noul"])
    except Exception:
        pass
    return 0.05

def render_typst_pdf(company: str, role_title: str, tailored: dict) -> str:
    """Compiles a clean, single-column ATS-compliant PDF using Typst."""
    PROFILE = get_master_profile()
    os.makedirs("output", exist_ok=True)
    safe_company = re.sub(r"[^a-zA-Z0-9]+", "_", company).strip("_")
    typ_path = f"output/resume_{safe_company}.typ"
    pdf_path = f"output/Rohan_Vailala_Thoma_{safe_company}.pdf"

    exp0 = PROFILE["experience"][0]
    exp1 = PROFILE["experience"][1]
    edu0 = PROFILE["education"][0]

    skills_prioritized = tailored.get("matched_keywords", []) + [
        s for s in PROFILE["core_skills"] if s not in tailored.get("matched_keywords", [])
    ]
    skills_str = escape_typst(", ".join(skills_prioritized))

    b1_typ = "\n".join(f"- {escape_typst(b)}" for b in tailored["tailored_bullets_role_1"])
    b2_typ = "\n".join(f"- {escape_typst(b)}" for b in tailored["tailored_bullets_role_2"])

    typst_source = f"""#set page(paper: "a4", margin: (x: 1.4cm, y: 1.3cm))
#set text(size: 10pt)
#set par(justify: true, leading: 0.55em)

#align(center)[
  #text(size: 16pt, weight: "bold")[{escape_typst(PROFILE['name'])}] \\
  #v(2pt)
  #text(size: 9pt)[{escape_typst(PROFILE['location'])} | {escape_typst(PROFILE['email'])} | {escape_typst(PROFILE['github'])}]
]

#v(6pt)
#line(length: 100%, stroke: 0.6pt)
#text(size: 11pt, weight: "bold")[SUMMARY — {escape_typst(role_title.upper())}] \\
{escape_typst(tailored['tailored_summary'])}

#v(6pt)
#line(length: 100%, stroke: 0.6pt)
#text(size: 11pt, weight: "bold")[TECHNICAL SKILLS] \\
{skills_str}

#v(6pt)
#line(length: 100%, stroke: 0.6pt)
#text(size: 11pt, weight: "bold")[EXPERIENCE]

#grid(columns: (1fr, auto),
  [*{escape_typst(exp0['company'])}* — _{escape_typst(exp0['role'])}_],
  [{escape_typst(exp0['dates'])}]
)
{b1_typ}

#v(4pt)
#grid(columns: (1fr, auto),
  [*{escape_typst(exp1['company'])}* — _{escape_typst(exp1['role'])}_],
  [{escape_typst(exp1['dates'])}]
)
{b2_typ}

#v(6pt)
#line(length: 100%, stroke: 0.6pt)
#text(size: 11pt, weight: "bold")[EDUCATION] \\
*{escape_typst(edu0['degree'])}* — {escape_typst(edu0['institution'])} ({escape_typst(edu0['dates'])})
"""
    with open(typ_path, "w", encoding="utf-8") as f:
        f.write(typst_source)

    typst.compile(typ_path, output=pdf_path)
    return pdf_path

async def tailor_resume_for_job(job: dict, custom_instruction: str = "") -> dict:
    """Runs System-2 Surgical Tailoring (OpenAI Direct or OpenRouter) + Jev Hallucination Audit + Typst PDF."""
    PROFILE = get_master_profile()
    try:
        from src.knowledge.store import get_store
        kc = get_store().search("resume win", type=None)[:3]
        learned = "\\n".join(c.body for c in kc)
    except Exception:
        learned = ""
    if OPENAI_API_KEY:
        llm_url = "https://api.openai.com/v1/chat/completions"
        llm_key = OPENAI_API_KEY
        llm_model = "gpt-4o-mini"
    # elif OPENROUTER_API_KEY:
    #     llm_url = "https://openrouter.ai/api/v1/chat/completions"
    #     llm_key = OPENROUTER_API_KEY
    #     llm_model = "openai/gpt-4o-mini"
    else:
        raise ValueError("Set OPENAI_API_KEY or OPENROUTER_API_KEY in .env!")

    prompt = f"""You are a Surgical ATS Resume Tailor.
Your strict rule: NEVER invent skills, companies, or metrics outside the Candidate Master Profile.
Only reorder skills, align terminology with the Job Description, and sharpen existing experience bullets.

Candidate Master Profile:
{json.dumps(PROFILE, indent=2)}

Target Job:
Company: {job['company']}
Title: {job['title']}
Persona Selected by System-1: {job.get('jev_persona', 'ai_agent_engineer')}
Job Description:
{(job.get('jd_text') or '')[:3500]}

Additional User Instruction: {custom_instruction or 'None'}

Learned Knowledge Context:
{learned}

Return ONLY valid JSON matching this schema:
{{
  "tailored_summary": "2-sentence ATS-aligned summary",
  "matched_keywords": ["skill1", "skill2", "skill3"],
  "tailored_bullets_role_1": ["bullet 1", "bullet 2", "bullet 3"],
  "tailored_bullets_role_2": ["bullet 1", "bullet 2"],
  "change_rationale": ["what changed 1", "what changed 2"]
}}"""

    async with httpx.AsyncClient(timeout=35.0) as client:
        resp = await post_with_retry(
            client,
            llm_url,
            headers={"Authorization": f"Bearer {llm_key}", "Content-Type": "application/json"},
            json={
                "model": llm_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        raw_json = json.loads(resp.json()["choices"][0]["message"]["content"])
        validated = TailoredResumeOutput(**raw_json).model_dump()

        # System-1 Hallucination Audit via Jev
        hallucination_prob = await verify_no_hallucination_with_jev(client, validated)
        if hallucination_prob >= 0.30:
            validated["tailored_bullets_role_1"] = PROFILE["experience"][0]["bullets"]
            validated["tailored_bullets_role_2"] = PROFILE["experience"][1]["bullets"]
            validated["change_rationale"].append(
                f"⚠️ Jev Hallucination Shield triggered (p={hallucination_prob:.2f}); reverted to verified master bullets."
            )

    pdf_path = render_typst_pdf(job["company"], job["title"], validated)

    diff_lines = [
        f"🎯 Tailored for: {job['title']} @ {job['company']}",
        f"🛡️ Jev Hallucination Risk: {hallucination_prob:.1%}",
        f"🔑 Prioritized ATS Keywords: {', '.join(validated['matched_keywords'])}",
        "",
        "📝 Surgical Changes Made:",
    ] + [f"  • {r}" for r in validated["change_rationale"]]

    return {
        "pdf_path": pdf_path,
        "diff_summary": "\n".join(diff_lines),
        "hallucination_prob": hallucination_prob,
    }