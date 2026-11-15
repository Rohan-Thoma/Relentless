import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re

from typing import Any

from dotenv import load_dotenv
import httpx
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator
import typst

from src.db import get_connection
from src.httpx_utils import post_with_retry
from src.settings_store import get_master_profile
from src.tailor.ats_scorer import compute_ats_scorecard, format_scorecard_markdown

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY", "").strip()

logger = logging.getLogger(__name__)


class SkillCategory(BaseModel):
    category: str = Field(
        description="Category header, e.g., 'GenAI & Agentic Systems', 'Computer Vision & Edge AI', 'Backend & Cloud MLOps'"
    )
    skills: list[str] = Field(description="Curated, relevant skills for this category")


class TailoredResumeOutput(BaseModel):
    tailored_summary: str = Field(
        description="3-sentence high-impact executive summary. Do not explicitly name the target job title; focus directly on the candidate's core expertise and achievements."
    )
    matched_keywords: list[str] = Field(description="Top 10-15 ATS keywords aligned with the JD.")
    skill_categories: list[SkillCategory] = Field(
        default_factory=list, description="3 to 5 organized skill categories tailored to the JD."
    )
    tailored_bullets_role_1: list[str] = Field(
        default_factory=list,
        description="Tailored bullets for experience[0], preserving all verified project accomplishments.",
    )
    tailored_bullets_role_2: list[str] = Field(
        default_factory=list,
        description="Tailored bullets for experience[1] (if present), preserving all verified project accomplishments.",
    )
    tailored_bullets_role_3: list[str] = Field(
        default_factory=list,
        description="Tailored bullets for experience[2] (if present), preserving all verified project accomplishments.",
    )
    tailored_bullets_by_role: list[list[str]] = Field(
        default_factory=list,
        description="Ordered list of tailored bullet arrays corresponding 1:1 with each entry in experience[].",
    )
    change_rationale: list[str] = Field(
        description="3 to 4 bullet points explaining specific rephrasings, keyword alignments, and domain nuances added."
    )

    @field_validator("skill_categories", mode="before")
    @classmethod
    def normalize_skill_categories(cls, v: Any) -> list[dict]:
        if isinstance(v, dict):
            if "categories" in v and isinstance(v["categories"], list):
                return cls.normalize_skill_categories(v["categories"])
            normalized = []
            for cat_name, skills_val in v.items():
                if isinstance(skills_val, list):
                    skills_list = [str(s).strip() for s in skills_val if str(s).strip()]
                elif isinstance(skills_val, str):
                    skills_list = [s.strip() for s in skills_val.split(",") if s.strip()]
                elif isinstance(skills_val, dict) and "skills" in skills_val:
                    raw = skills_val["skills"]
                    skills_list = raw if isinstance(raw, list) else [s.strip() for s in str(raw).split(",") if s.strip()]
                else:
                    skills_list = [str(skills_val)]
                normalized.append({"category": str(cat_name), "skills": skills_list})
            return normalized

        if isinstance(v, list):
            normalized = []
            for item in v:
                if isinstance(item, dict):
                    if "category" in item and "skills" in item:
                        raw_skills = item["skills"]
                        skills_list = (
                            raw_skills if isinstance(raw_skills, list)
                            else [s.strip() for s in str(raw_skills).split(",") if s.strip()]
                        )
                        normalized.append({"category": str(item["category"]), "skills": skills_list})
                    elif len(item) == 1:
                        cat_name, skills_val = next(iter(item.items()))
                        skills_list = skills_val if isinstance(skills_val, list) else [str(skills_val)]
                        normalized.append({"category": str(cat_name), "skills": skills_list})
                    else:
                        normalized.append(item)
                elif isinstance(item, str):
                    if ":" in item:
                        cat_name, rest = item.split(":", 1)
                        skills_list = [s.strip() for s in rest.split(",") if s.strip()]
                        normalized.append({"category": cat_name.strip(), "skills": skills_list})
                    else:
                        normalized.append({"category": "Core Skills", "skills": [item.strip()]})
                else:
                    normalized.append(item)
            return normalized

        return []

    @field_validator("change_rationale", mode="before")
    @classmethod
    def normalize_change_rationale(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            lines = [line.strip().lstrip("-•* ").strip() for line in v.split("\n") if line.strip()]
            return lines if lines else [v.strip()]
        if isinstance(v, list):
            return [str(item).strip() for item in v if str(item).strip()]
        if isinstance(v, dict):
            return [f"{k}: {val}" for k, val in v.items()]
        return [str(v)]

    @field_validator("matched_keywords", mode="before")
    @classmethod
    def normalize_matched_keywords(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        if isinstance(v, list):
            return [str(item).strip() for item in v if str(item).strip()]
        return []

    @field_validator("tailored_bullets_role_1", "tailored_bullets_role_2", "tailored_bullets_role_3", mode="before")
    @classmethod
    def normalize_bullets(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            lines = [line.strip().lstrip("-•* ").strip() for line in v.split("\n") if line.strip()]
            return lines if lines else [v.strip()]
        if isinstance(v, list):
            return [str(item).strip() for item in v if str(item).strip()]
        return []

    @field_validator("tailored_bullets_by_role", mode="before")
    @classmethod
    def normalize_bullets_by_role(cls, v: Any) -> list[list[str]]:
        if not isinstance(v, list):
            return []
        out: list[list[str]] = []
        for entry in v:
            out.append(cls.normalize_bullets(entry))
        return out

    @field_validator("tailored_summary", mode="before")
    @classmethod
    def normalize_summary(cls, v: Any) -> str:
        if isinstance(v, list):
            return " ".join(str(s).strip() for s in v if str(s).strip())
        return str(v or "")


class AuditUnavailable(Exception):
    """Raised when the hallucination audit cannot be performed."""


def escape_typst(text: str) -> str:
    """Escapes special Typst markup characters so compilation never fails."""
    if not text:
        return ""
    for ch in ["\\", "#", "$", "@", "*", "_", "<", ">", "`", "[", "]"]:
        text = text.replace(ch, "\\" + ch)
    return text


async def verify_no_hallucination_with_jev(
    client: httpx.AsyncClient, tailored: dict, custom_instruction: str = ""
) -> float:
    """Uses Jev System-1 noul primitive to verify that the tailored resume does not fabricate false credentials."""
    url = "https://api.typesafe.ai/v1/systemone" if TYPESAFE_API_KEY else "https://openrouter.ai/api/alpha/decisions"
    api_key = TYPESAFE_API_KEY if TYPESAFE_API_KEY else OPENROUTER_API_KEY
    model_id = "jev-1.13.0" if TYPESAFE_API_KEY else "typesafe/jev-1.14"

    if not api_key:
        raise AuditUnavailable("No API key configured for hallucination audit.")

    PROFILE = get_master_profile()

    payload = {
        "model": model_id,
        "state": {
            "verified_candidate_profile": {
                "allowed_skills": PROFILE["core_skills"],
                "original_experience": PROFILE["experience"],
            },
            "user_explicit_instruction": custom_instruction,
            "proposed_tailored_output": tailored,
        },
        "questions": {
            "has_hallucination": {
                "type": "noul",
                "instructions": "Does proposed_tailored_output fabricate false credentials, fake companies, or completely unrelated technology stacks outside the candidate's verified profile?",
                "criteria": {
                    "true": "Fabricates fake employers, fake degrees, or claims deep expertise in a completely unrelated domain (e.g., iOS Swift, COBOL, Kernel Driver C) not present in the profile and not requested by user_explicit_instruction.",
                    "false": "Rephrases existing projects to align with JD terminology, fixes grammar, incorporates natural adjacent ecosystem tools (e.g., LangSmith/observability for LangGraph, quantization for TensorRT, REST/webhooks for FastAPI), or follows user_explicit_instruction.",
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
        if resp.status_code != 200:
            raise AuditUnavailable(f"Audit HTTP {resp.status_code}")
        data = resp.json()
        return float(data["answers"]["has_hallucination"]["noul"])
    except (KeyError, ValueError, TypeError) as e:
        raise AuditUnavailable(f"Malformed audit response: {e}")
    except AuditUnavailable:
        raise
    except Exception as e:
        raise AuditUnavailable(f"Audit request failed: {e}")


def render_typst_pdf(company: str, role_title: str, tailored: dict, job_id: int | None = None) -> str:
    """Compiles a clean, single-column ATS-compliant PDF using Typst for arbitrary role counts."""
    PROFILE = get_master_profile()
    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_company = re.sub(r"[^a-zA-Z0-9]+", "_", company or "Company").strip("_") or "Company"
    safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", str(PROFILE.get("name") or "Candidate")).strip("_") or "Candidate"
    suffix = f"_{job_id}" if job_id is not None else ""
    typ_path = str(output_dir / f"resume_{safe_company}{suffix}.typ")
    pdf_path = str(output_dir / f"{safe_name}_{safe_company}{suffix}.pdf")

    experiences = PROFILE.get("experience") or []
    if not isinstance(experiences, list) or len(experiences) == 0:
        raise ValueError(
            "Master profile must contain at least 1 experience entry to generate a tailored resume PDF."
        )

    # Render skill categories cleanly
    skill_cats = tailored.get("skill_categories", [])
    if skill_cats:
        cats_typ_lines = []
        for sc in skill_cats:
            cat_name = escape_typst(sc.get("category", "") if isinstance(sc, dict) else sc.category)
            skills_list = sc.get("skills", []) if isinstance(sc, dict) else sc.skills
            skills_joined = escape_typst(", ".join(skills_list))
            cats_typ_lines.append(f"- *{cat_name}:* {skills_joined}")
        skills_block = "\n".join(cats_typ_lines)
    else:
        skills_prioritized = tailored.get("matched_keywords", []) + [
            s for s in PROFILE.get("core_skills", []) if s not in tailored.get("matched_keywords", [])
        ]
        skills_block = escape_typst(", ".join(skills_prioritized))

    by_role = tailored.get("tailored_bullets_by_role")
    exp_blocks: list[str] = []
    for idx, exp in enumerate(experiences):
        bullets: list[str] = []
        if isinstance(by_role, list) and idx < len(by_role) and isinstance(by_role[idx], list) and by_role[idx]:
            bullets = by_role[idx]
        elif idx < 3:
            bullets = list(tailored.get(f"tailored_bullets_role_{idx + 1}") or [])
        if not bullets and (idx >= 3 or (not tailored.get("tailored_bullets_role_1") and not by_role)):
            bullets = list(exp.get("bullets") or [])

        bullets_typ = "\n".join(f"- {escape_typst(b)}" for b in bullets)
        spacer = "\n#v(3pt)\n" if idx > 0 else ""
        exp_blocks.append(
            f"""{spacer}#grid(columns: (1fr, auto),
  [*{escape_typst(exp.get('company', 'Company'))}* — _{escape_typst(exp.get('role', 'Role'))}_],
  [{escape_typst(exp.get('dates', ''))}]
)
{bullets_typ}"""
        )
    exp_section = "\n".join(exp_blocks)

    # Clickable hyperlinks for email, linkedin, github
    li_url = PROFILE.get("linkedin", "")
    if li_url:
        li_target = li_url if li_url.startswith("http") else "https://" + li_url
        li_elem = f'#link("{li_target}")[{escape_typst(li_url)}]'
    else:
        li_elem = ""

    gh_url = PROFILE.get("github", "")
    if gh_url:
        gh_target = gh_url if gh_url.startswith("http") else "https://" + gh_url
        gh_elem = f'#link("{gh_target}")[{escape_typst(gh_url)}]'
    else:
        gh_elem = ""

    email = PROFILE.get("email", "")
    email_elem = f'#link("mailto:{email}")[{escape_typst(email)}]' if email else ""

    contact_parts: list[str] = []
    if PROFILE.get("location"):
        contact_parts.append(escape_typst(PROFILE["location"]))
    if PROFILE.get("phone"):
        contact_parts.append(escape_typst(PROFILE["phone"]))
    if email_elem:
        contact_parts.append(email_elem)
    if li_elem:
        contact_parts.append(li_elem)
    if gh_elem:
        contact_parts.append(gh_elem)
    contact_line = " | ".join(contact_parts)

    educations = PROFILE.get("education") or []
    edu_section = ""
    if isinstance(educations, list) and educations:
        edu_lines = []
        for edu in educations:
            deg = escape_typst(str(edu.get("degree") or "Degree"))
            inst = escape_typst(str(edu.get("institution") or "Institution"))
            dates = escape_typst(str(edu.get("dates") or ""))
            date_suffix = f" ({dates})" if dates else ""
            edu_lines.append(f"*{deg}* — {inst}{date_suffix}")
        edu_section = (
            "\n#v(4pt)\n#line(length: 100%, stroke: 0.5pt)\n"
            '#text(size: 10.5pt, weight: "bold")[EDUCATION] \\\n'
            + " \\\n".join(edu_lines)
        )

    typst_source = f"""#set page(paper: "a4", margin: (x: 1.2cm, y: 1.0cm))
#set text(size: 9.5pt)
#set par(justify: true, leading: 0.52em)

#align(center)[
  #text(size: 15pt, weight: "bold")[{escape_typst(PROFILE.get('name', 'Candidate'))}] \\
  #v(2pt)
  #text(size: 8.5pt)[{contact_line}]
]

#v(4pt)
#line(length: 100%, stroke: 0.5pt)
#text(size: 10.5pt, weight: "bold")[EXECUTIVE SUMMARY] \\
{escape_typst(tailored.get('tailored_summary', ''))}

#v(4pt)
#line(length: 100%, stroke: 0.5pt)
#text(size: 10.5pt, weight: "bold")[EXPERIENCE]

{exp_section}

#v(4pt)
#line(length: 100%, stroke: 0.5pt)
#text(size: 10.5pt, weight: "bold")[TECHNICAL SKILLS]
{skills_block}{edu_section}
"""
    with open(typ_path, "w", encoding="utf-8") as f:
        f.write(typst_source)

    typst.compile(typ_path, output=pdf_path)
    return pdf_path


async def ensure_tailored_pdf(job_row: dict) -> str | None:
    """
    Checks if job_row.tailored_pdf_path exists on disk.
    If container restarted and wiped output/, regenerates PDF from stored tailored_resume_json
    with 0 LLM calls!
    """
    existing_path = job_row.get("tailored_pdf_path")
    if existing_path and Path(existing_path).exists():
        return existing_path

    tailored_json = job_row.get("tailored_resume_json")
    if not tailored_json:
        return existing_path if (existing_path and Path(existing_path).exists()) else None

    company = job_row.get("company", "Company")
    title = job_row.get("title", "Role")
    job_id = job_row.get("id")

    pdf_path = await asyncio.to_thread(render_typst_pdf, company, title, tailored_json, job_id)
    return pdf_path


async def tailor_resume_for_job(job: dict, custom_instruction: str = "") -> dict:
    """Runs System-2 Surgical Tailoring (OpenAI gpt-4o-mini) + Jev Hallucination Audit + Typst PDF."""
    PROFILE = get_master_profile()
    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is required to generate surgical resume tailoring!")

    # Format verified background for the system prompt
    exp_summary = []
    role_rules = []
    experiences = PROFILE.get("experience") or []
    if not experiences:
        raise ValueError(
            "Master profile has no experience entries; at least 1 experience role is required for resume tailoring."
        )
    for idx, e in enumerate(experiences, 1):
        bullets_list = e.get("bullets") or []
        bullets = "\n".join(f"  - {b}" for b in bullets_list)
        exp_summary.append(
            f"Role {idx}: {e.get('role', 'Role')} at {e.get('company', 'Company')} ({e.get('dates', '')})\n{bullets}"
        )
        hint = f"preserve depth (~{len(bullets_list)} comprehensive bullets)" if bullets_list else "3 to 5 comprehensive bullets"
        role_rules.append(
            f"   - Role {idx} ({e.get('company', 'Company')} — {e.get('role', 'Role')}): {hint}."
        )
    verified_exp_text = "\n\n".join(exp_summary)
    role_rules_text = "\n".join(role_rules)

    user_instruction_block = (
        f"\nUSER CUSTOM INSTRUCTION / REVISION REQUEST:\n{custom_instruction}\n" if custom_instruction else ""
    )

    system_prompt = f"""You are an expert ATS Resume Strategist and Principal Software/AI Engineer.
Your task is to surgically tailor the candidate's verified resume for the following job description.

STRICT CONSTRAINTS (ZERO-HALLUCINATION POLICY):
1. NEVER fabricate employers, fake degrees, or fake metrics.
2. Every tailored bullet must be anchored in the candidate's actual, verified projects.
3. You MAY rephrase bullets to highlight relevant keywords from the JD cleanly and accurately.
4. Preserve depth across each experience role in `tailored_bullets_by_role` (matching all {len(experiences)} role(s) in order) AND populate `tailored_bullets_role_1`..`role_3` for up to the first 3 roles:
{role_rules_text}
5. Technical skills must be categorized into 3-5 intuitive groups relevant to the candidate's stack and the JD.
6. Provide a 3-sentence high-impact executive summary.

CANDIDATE'S VERIFIED BACKGROUND:
Name: {PROFILE.get('name', 'Candidate')}
Core Skills: {', '.join(PROFILE.get('core_skills', []))}

Verified Experience:
{verified_exp_text}
{user_instruction_block}
TARGET JOB:
Company: {job['company']}
Title: {job['title']}
Location: {job['location']}
Job Description:
{job.get('jd_text', '')[:4000]}
"""

    prompt = """Generate the ATS-optimized JSON payload for this candidate.
Return strictly valid JSON matching this schema:
{
  "tailored_summary": "3-sentence executive summary...",
  "matched_keywords": ["keyword1", "keyword2", ...],
  "skill_categories": [
    {"category": "Category Name 1", "skills": ["skill1", "skill2", ...]},
    {"category": "Category Name 2", "skills": ["skill1", "skill2", ...]}
  ],
  "tailored_bullets_by_role": [["bullet 1", ...]],
  "tailored_bullets_role_1": ["bullet 1", "bullet 2", ...],
  "tailored_bullets_role_2": ["bullet 1", "bullet 2", ...],
  "tailored_bullets_role_3": ["bullet 1", "bullet 2", ...],
  "change_rationale": ["rationale point 1", "rationale point 2", ...]
}"""

    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await post_with_retry(
            client,
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"OpenAI tailoring failed: {resp.status_code} {resp.text}")

        tailored_data = json.loads(resp.json()["choices"][0]["message"]["content"])
        validated = TailoredResumeOutput(**tailored_data).model_dump()

        # Phase 2: Jev Hallucination Audit
        hallucination_prob = 0.0
        try:
            hallucination_prob = await verify_no_hallucination_with_jev(client, validated, custom_instruction)
        except AuditUnavailable as e:
            logger.warning("Jev audit unavailable, proceeding without audit score: %s", e)
            hallucination_prob = 0.0

        if hallucination_prob > 0.45:
            logger.warning(
                "Jev Hallucination Audit detected high risk (%.2f) for job %s! Triggering single corrective pass.",
                hallucination_prob,
                job.get("id"),
            )
            corrective_prompt = f"""Jev System-1 Hallucination Audit reported that the tailored resume may contain exaggerated or fabricated claims (risk prob={hallucination_prob:.2f}).
Please tone down any over-claiming, ensure every claim directly matches the candidate's verified accomplishments, and remove any unsupported tool claims while retaining ATS keyword alignment."""

            try:
                corr_resp = await post_with_retry(
                    client,
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "assistant", "content": json.dumps(validated)},
                            {"role": "user", "content": corrective_prompt},
                        ],
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                    },
                )
                if corr_resp.status_code == 200:
                    corr_json = json.loads(corr_resp.json()["choices"][0]["message"]["content"])
                    validated = TailoredResumeOutput(**corr_json).model_dump()
            except Exception as e:
                logger.warning("Corrective LLM pass failed: %s", e)

            validated["change_rationale"].append(
                f"⚠️ Jev Audit noted high stretch (p={hallucination_prob:.2f}) — please review highlighted claims before submitting."
            )

    pdf_path = await asyncio.to_thread(render_typst_pdf, job["company"], job["title"], validated, job.get("id"))

    with open(pdf_path, "rb") as f:
        pdf_sha256 = hashlib.sha256(f.read()).hexdigest()

    ats_scorecard = compute_ats_scorecard(job.get("jd_text") or "", PROFILE, validated)
    scorecard_md = format_scorecard_markdown(ats_scorecard)

    diff_lines = [
        f"🎯 Tailored for: {job['title']} @ {job['company']}",
        f"🛡️ Jev Hallucination Risk: {hallucination_prob:.1%}",
        f"🔑 Prioritized ATS Keywords: {', '.join(validated['matched_keywords'])}",
        "",
        "📝 Surgical Changes Made:",
    ] + [f"  • {r}" for r in validated["change_rationale"]]

    diff_summary = f"{scorecard_md}\n\n" + "\n".join(diff_lines)

    if job.get("id"):
        def _persist_tailored():
            with get_connection() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE jobs
                    SET tailored_pdf_path = %s,
                        tailored_pdf_sha256 = %s,
                        tailored_diff = %s,
                        ats_scorecard = %s,
                        tailored_resume_json = %s
                    WHERE id = %s;
                    """,
                    (pdf_path, pdf_sha256, diff_summary, Jsonb(ats_scorecard), Jsonb(validated), job["id"]),
                )
                conn.commit()

        try:
            await asyncio.to_thread(_persist_tailored)
        except Exception as e:
            logger.warning("Could not persist tailored resume to jobs: %s", e)

    return {
        "pdf_path": pdf_path,
        "pdf_sha256": pdf_sha256,
        "diff_summary": diff_summary,
        "hallucination_prob": hallucination_prob,
        "tailored": validated,
        "ats_scorecard": ats_scorecard,
    }
