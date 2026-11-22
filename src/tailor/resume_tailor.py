from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any

from dotenv import load_dotenv
import httpx
from pydantic import BaseModel, Field, field_validator
import typst

from src.config import load_config
from src.httpx_utils import post_with_retry
from src.jd_parse import STATIC_SKILLS, skill_in_text
from src.model_catalog import resolve_chat_endpoint, resolve_triage_endpoint
from src.settings_store import get_master_profile

load_dotenv()

logger = logging.getLogger(__name__)

# Natural ecosystem bridges allowed automatically when the candidate has the parent skill.
# Always highlighted transparently in the Tailor Review Card diff!
ADJACENT_SKILL_BRIDGES: dict[str, set[str]] = {
    "pandas": {"numpy", "data analysis", "data preprocessing", "eda", "tabular data"},
    "numpy": {"scipy", "linear algebra", "vectorized computing"},
    "pytorch": {"deep learning", "neural networks", "model training", "gpu acceleration", "torchvision", "fine-tuning"},
    "tensorflow": {"keras", "deep learning", "neural networks", "model training"},
    "fastapi": {"rest", "rest apis", "restful apis", "openapi", "pydantic", "webhooks", "async python", "microservices"},
    "flask": {"rest", "rest apis", "wsgi", "web apis"},
    "docker": {"containerization", "containers", "dockerfile", "docker compose"},
    "langgraph": {"agentic workflows", "multi-agent", "multi-agent orchestration", "langsmith", "stateful agents", "tool calling"},
    "langchain": {"llm orchestration", "prompt engineering", "rag", "rag pipelines", "embeddings", "chunking"},
    "rag": {"retrieval", "hybrid search", "vector search", "semantic search", "chunking", "reranking", "context engineering"},
    "llms": {"generative ai", "genai", "prompt engineering", "structured outputs", "function calling", "inference"},
    "tensorrt": {"quantization", "model optimization", "inference optimization", "fp16", "int8", "edge inference", "latency optimization"},
    "onnx": {"model conversion", "cross-platform inference", "onnx runtime", "edge deployment"},
    "yolo": {"yolov8", "object detection", "real-time detection", "computer vision", "bounding box detection"},
    "opencv": {"image processing", "video analytics", "computer vision"},
    "postgresql": {"sql", "relational databases", "query optimization", "indexing"},
    "pgvector": {"vector database", "vector databases", "vector search", "embeddings", "hnsw"},
    "aws ec2": {"aws", "cloud infrastructure", "linux deployment"},
    "aws bedrock": {"aws", "managed llms", "cloud ai"},
    "ci/cd": {"github actions", "automated testing", "deployment pipelines", "devops"},
    "mlflow": {"experiment tracking", "model registry", "mlops"},
    "apache airflow": {"workflow orchestration", "dags", "etl", "data pipelines"},
    "python": {"asyncio", "oop", "scripting", "backend engineering"},
}

# Major independent platforms/languages that must NEVER be silently fabricated without
# being in master_profile.json or explicitly requested by the user's custom_instruction.
MAJOR_STANDALONE_PLATFORMS: set[str] = {
    "kubernetes", "k8s", "helm", "terraform", "ansible",
    "spark", "pyspark", "hadoop", "flink", "kafka", "snowflake", "databricks", "dbt",
    "rust", "go", "golang", "scala", "java", "c#", ".net", "swift", "kotlin", "ruby", "php", "elixir",
    "react", "angular", "vue", "next.js", "typescript",
    "gcp", "vertex ai", "bigquery", "azure ml", "sagemaker",
}

METRIC_TOKEN_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|x|fps|ms|hrs?|hours?|mins?|minutes?|lpa|k|m|\+)\b",
    re.IGNORECASE,
)


class SkillCategory(BaseModel):
    category: str = Field(
        description="Category header, e.g., 'GenAI & Agentic Systems', 'Computer Vision & Edge AI', 'Backend & Cloud MLOps'"
    )
    skills: list[str] = Field(description="Curated, relevant skills for this category")


class TailoredResumeOutput(BaseModel):
    tailored_summary: str = Field(
        description="3-4 sentence high-impact technical summary anchored directly in verified work and the target domain. Avoid generic clichés like 'Results-driven'."
    )
    matched_keywords: list[str] = Field(
        default_factory=list, description="Top 10-18 ATS keywords aligned with the JD."
    )
    skill_categories: list[SkillCategory] = Field(
        default_factory=list, description="3 to 6 organized skill categories tailored to the JD."
    )
    tailored_bullets_role_1: list[str] = Field(default_factory=list)
    tailored_bullets_role_2: list[str] = Field(default_factory=list)
    tailored_bullets_role_3: list[str] = Field(default_factory=list)
    tailored_bullets_by_role: list[list[str]] = Field(
        default_factory=list,
        description="Ordered list of tailored bullet arrays corresponding 1:1 with each entry in experience[].",
    )
    change_rationale: list[str] = Field(
        default_factory=list,
        description="3 to 5 bullet points explaining specific rephrasings, keyword alignments, and adjacent bridges.",
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
        return [cls.normalize_bullets(entry) for entry in v]

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


def _profile_corpus_lower(profile: dict[str, Any]) -> str:
    parts = [
        str(profile.get("experience_summary") or ""),
        " ".join(str(s) for s in (profile.get("core_skills") or [])),
    ]
    for exp in profile.get("experience") or []:
        parts.append(str(exp.get("role") or ""))
        parts.extend(str(b) for b in (exp.get("bullets") or []))
    return " ".join(parts).lower()


def classify_tailored_skills(
    profile: dict[str, Any],
    tailored: dict[str, Any],
    custom_instruction: str = "",
) -> dict[str, list[str]]:
    """3-Tier Skill Transparency Classifier:
    - verified: present in master_profile.json (or explicitly requested via custom_instruction)
    - adjacent_bridged: natural ecosystem bridge from a verified skill (allowed & labeled in diff)
    - unverified_stretch: standalone major platform NOT in profile and NOT requested by user
    """
    prof_text = _profile_corpus_lower(profile)
    instr_low = (custom_instruction or "").lower()

    allowed_adjacent: set[str] = set()
    for parent, children in ADJACENT_SKILL_BRIDGES.items():
        if skill_in_text(parent, prof_text):
            allowed_adjacent.update(children)

    emitted_skills: list[str] = []
    for cat in tailored.get("skill_categories") or []:
        raw_list = cat.get("skills", []) if isinstance(cat, dict) else getattr(cat, "skills", [])
        for s in raw_list:
            clean = str(s).strip()
            if clean and clean not in emitted_skills:
                emitted_skills.append(clean)

    verified: list[str] = []
    adjacent_bridged: list[str] = []
    unverified_stretch: list[str] = []

    for skill in emitted_skills:
        s_low = skill.lower()
        if skill_in_text(s_low, prof_text) or (instr_low and skill_in_text(s_low, instr_low)):
            verified.append(skill)
        elif s_low in allowed_adjacent or any(skill_in_text(a, s_low) for a in allowed_adjacent):
            adjacent_bridged.append(skill)
        elif s_low in MAJOR_STANDALONE_PLATFORMS or any(skill_in_text(m, s_low) for m in MAJOR_STANDALONE_PLATFORMS):
            unverified_stretch.append(skill)
        else:
            # Fine-grained domain phrase/concept (e.g. 'Latency Profiling') -> adjacent bridged
            adjacent_bridged.append(skill)

    return {
        "verified": verified,
        "adjacent_bridged": adjacent_bridged,
        "unverified_stretch": unverified_stretch,
    }


def check_preservation(
    profile: dict[str, Any],
    jd_text: str,
    tailored: dict[str, Any],
    custom_instruction: str = "",
) -> dict[str, Any]:
    """Deterministic preservation & integrity check used by TailorGraph's cyclic refinement loop.

    Checks:
    1. JD-relevant verified core skills from master_profile were NOT dropped from rendered skills/text.
    2. Quantified metrics (percentages, FPS, multipliers) from original bullets were NOT stripped.
    3. Verified experience bullets were NOT silently truncated/deleted.
    4. Major standalone unverified platforms (e.g. Kubernetes, Rust) were NOT silently fabricated
       unless explicitly requested by custom_instruction.
    """
    violations: list[str] = []
    jd_low = (jd_text or "").lower()
    instr_low = (custom_instruction or "").lower()

    # Build rendered text representation
    skill_text_parts = []
    for cat in tailored.get("skill_categories") or []:
        if isinstance(cat, dict):
            skill_text_parts.extend(str(s) for s in (cat.get("skills") or []))
    rendered_skills_low = " ".join(skill_text_parts).lower()

    by_role = tailored.get("tailored_bullets_by_role") or []
    all_tailored_bullets: list[str] = []
    if by_role and any(by_role):
        for r in by_role:
            all_tailored_bullets.extend(str(b) for b in (r or []))
    else:
        for i in range(1, 4):
            all_tailored_bullets.extend(str(b) for b in (tailored.get(f"tailored_bullets_role_{i}") or []))

    rendered_full_low = (
        str(tailored.get("tailored_summary") or "")
        + " "
        + rendered_skills_low
        + " "
        + " ".join(all_tailored_bullets)
    ).lower()

    # 1. Core JD-relevant profile skills must NOT be deleted!
    missing_core_skills: list[str] = []
    for skill in profile.get("core_skills") or []:
        s_low = str(skill).strip().lower()
        if not s_low:
            continue
        is_mandatory_lang = s_low in {"python", "pytorch", "sql", "docker", "fastapi", "postgresql"}
        if (skill_in_text(s_low, jd_low) or is_mandatory_lang) and s_low in STATIC_SKILLS:
            if not skill_in_text(s_low, rendered_skills_low) and not skill_in_text(s_low, rendered_full_low):
                missing_core_skills.append(str(skill))
    if missing_core_skills:
        violations.append(
            f"Deleted JD-relevant core profile skills: {', '.join(missing_core_skills[:8])}. Restore them in skill_categories!"
        )

    # 2. Check bullet count preservation per role (never silently chop 7 bullets down to 4!)
    experiences = profile.get("experience") or []
    for idx, exp in enumerate(experiences):
        orig_bullets = exp.get("bullets") or []
        if not orig_bullets:
            continue
        new_bullets = (
            by_role[idx]
            if (idx < len(by_role) and isinstance(by_role[idx], list) and by_role[idx])
            else (tailored.get(f"tailored_bullets_role_{idx + 1}") or [])
        )
        # Allow dropping at most 1 clearly irrelevant bullet if role has >= 5 bullets, unless user explicitly asked to shorten
        min_expected = max(1, len(orig_bullets) - (1 if len(orig_bullets) >= 5 else 0))
        if "shorten" not in instr_low and "condense" not in instr_low and "1 page" not in instr_low:
            if len(new_bullets) < min_expected:
                violations.append(
                    f"Role {idx + 1} ({exp.get('company', 'Role')}) lost bullets: had {len(orig_bullets)}, output only {len(new_bullets)}. Keep full project depth (multi-page PDF is allowed)."
                )

    # 3. Check quantified metric survival (e.g., 95%, 90%, 15 FPS, 50x, 100+)
    orig_metrics: set[str] = set()
    for exp in experiences:
        for b in exp.get("bullets") or []:
            for m in METRIC_TOKEN_RE.findall(str(b)):
                orig_metrics.add(re.sub(r"\s+", "", m.lower()))

    tailored_metrics_norm = {
        re.sub(r"\s+", "", m.lower())
        for b in all_tailored_bullets
        for m in METRIC_TOKEN_RE.findall(str(b))
    }
    if orig_metrics:
        dropped_metrics = sorted(m for m in orig_metrics if m not in tailored_metrics_norm)
        survival_ratio = (len(orig_metrics) - len(dropped_metrics)) / len(orig_metrics)
        if survival_ratio < 0.70 and len(dropped_metrics) >= 2:
            violations.append(
                f"Stripped verified quantified metrics ({', '.join(dropped_metrics[:6])}). Keep all original concrete metrics and benchmark numbers!"
            )

    # 4. 3-Tier skill transparency check (flag unrequested major standalone platforms)
    tier_info = classify_tailored_skills(profile, tailored, custom_instruction=custom_instruction)
    if tier_info["unverified_stretch"]:
        violations.append(
            f"Silently injected unverified standalone platform(s): {', '.join(tier_info['unverified_stretch'])}. Remove them from skill_categories unless explicitly requested by user."
        )

    # 5. Ban lazy ChatGPT cliché opener
    summary_low = str(tailored.get("tailored_summary") or "").strip().lower()
    if summary_low.startswith(("results-driven ", "dynamic and results-driven ", "passionate and results-driven ")):
        violations.append(
            "Executive summary starts with generic cliché ('Results-driven...'). Rewrite with specific engineering impact tied directly to the target company's domain."
        )

    return {
        "passed": len(violations) == 0,
        "violations": violations,
        "skill_tiers": tier_info,
        "missing_core_skills": missing_core_skills,
    }


async def call_tailor_llm(
    client: httpx.AsyncClient,
    job: dict[str, Any],
    profile: dict[str, Any],
    custom_instruction: str = "",
    okf_edit_habits: str = "",
    refinement_feedback: str = "",
    prior_draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single LLM tailoring pass using the user-configured model (defaults to openai / gpt-4o)."""
    cfg = load_config()
    url, api_key, model_id = resolve_chat_endpoint(cfg.models.tailor.provider, cfg.models.tailor.model)
    if not url or not api_key:
        raise ValueError("Configure OPENAI_API_KEY or OPENROUTER_API_KEY for resume tailoring!")

    experiences = profile.get("experience") or []
    if not experiences:
        raise ValueError(
            "Master profile has no experience entries; at least 1 experience role is required for resume tailoring."
        )

    exp_summary = []
    role_rules = []
    for idx, e in enumerate(experiences, 1):
        bullets_list = e.get("bullets") or []
        bullets = "\n".join(f"  - {b}" for b in bullets_list)
        loc_part = f" | {e.get('Location') or e.get('location')}" if (e.get("Location") or e.get("location")) else ""
        exp_summary.append(
            f"Role {idx}: {e.get('role', 'Role')} at {e.get('company', 'Company')} ({e.get('dates', '')}{loc_part})\n{bullets}"
        )
        role_rules.append(
            f"   - Role {idx} ({e.get('company', 'Company')} — {e.get('role', 'Role')}): preserve ALL {len(bullets_list)} verified project bullets and EVERY quantified metric."
        )

    verified_exp_text = "\n\n".join(exp_summary)
    role_rules_text = "\n".join(role_rules)
    persona = job.get("jev_persona") or "ai_agent_engineer"

    persona_guidance = {
        "ai_agent_engineer": "Emphasize LLM systems, RAG, LangGraph multi-agent orchestration, VLM pipelines, evaluation, and production AI deployment.",
        "backend_python_engineer": "Emphasize Python backend architecture, FastAPI/Flask services, high-throughput inference servers, Gunicorn/Nginx load balancing, PostgreSQL, Docker, and cloud infrastructure.",
        "data_ml_engineer": "Emphasize end-to-end ML pipelines, SQL/PostgreSQL, data versioning, clustering/calibration algorithms, ETL automation, and measurable business ROI.",
    }.get(persona, "Emphasize end-to-end engineering ownership, production ML/AI deployment, and measurable technical impact.")

    habits_block = (
        f"\nLEARNED USER TAILORING PREFERENCES (from past edits — apply these principles proactively):\n{okf_edit_habits}\n"
        if okf_edit_habits
        else ""
    )
    user_instruction_block = (
        f"\nEXPLICIT USER INSTRUCTION (HIGHEST PRIORITY — OBEY UNCONDITIONALLY):\n{custom_instruction}\n"
        if custom_instruction
        else ""
    )

    # Full untruncated JD (only safety guard at 24k chars for giant dumps)
    full_jd = (job.get("jd_text") or "")[:24000]

    system_prompt = f"""You are an elite Principal Engineering Resume Strategist.
Tailor the candidate's verified resume for the target job description to maximize both real ATS match and hiring-manager technical credibility.

CORE RULES:
1. NEVER shrink, compress away, or drop the candidate's verified projects, concrete tech stack (e.g. DeepSeek-VL2, TensorRT, ONNX, FastAPI, Gunicorn, Nginx, Jetson Nano), or quantified metrics (e.g. 95%, 90%, 15 FPS, 50x, 100+ hrs). Multi-page resumes (1–3 pages) are completely welcome — depth beats brevity!
2. NEVER delete JD-relevant core skills present in the candidate's profile (especially core languages & frameworks like Python, PyTorch, SQL, Docker, FastAPI).
3. You MAY include natural adjacent ecosystem terminology directly implied by the candidate's stack (e.g., REST APIs for FastAPI, Containerization for Docker, NumPy alongside Pandas/PyTorch, Inference Optimization for TensorRT) to outsmart rigid ATS parsers.
4. Do NOT silently inject major unrelated standalone platforms (e.g. Kubernetes, Spark, Rust, Scala, Java) unless present in the profile OR explicitly requested in EXPLICIT USER INSTRUCTION.
5. NEVER fabricate employers, job titles, dates, degrees, or fake numbers.
6. NEVER start the summary with generic clichés like "Results-driven..." or "Passionate...". Anchor the 3-4 sentence summary directly in concrete systems built and the target employer's domain.
7. Preserve ALL {len(experiences)} role(s) in order inside `tailored_bullets_by_role`:
{role_rules_text}
8. Organize Technical Skills into 4-5 sharp, JD-aligned categories covering all JD-relevant profile skills plus natural adjacent ecosystem terms.
9. TARGET PERSONA FOCUS (`{persona}`): {persona_guidance}
{habits_block}{user_instruction_block}
CANDIDATE'S VERIFIED BACKGROUND:
Name: {profile.get('name', 'Candidate')}
Core Skills: {', '.join(profile.get('core_skills', []))}
Original Summary: {profile.get('experience_summary', '')}

Verified Experience:
{verified_exp_text}

TARGET JOB:
Company: {job.get('company', 'Company')}
Title: {job.get('title', 'Role')}
Location: {job.get('location', '')}
Full Job Description:
{full_jd}
"""

    prompt = """Generate the tailored resume JSON payload.
Return strictly valid JSON matching this schema:
{
  "tailored_summary": "3-4 sentence concrete technical summary (NO 'Results-driven' cliché)...",
  "matched_keywords": ["keyword1", "keyword2", ...],
  "skill_categories": [
    {"category": "Category Name 1", "skills": ["skill1", "skill2", ...]},
    {"category": "Category Name 2", "skills": ["skill1", "skill2", ...]}
  ],
  "tailored_bullets_by_role": [
    ["Role 1 bullet 1...", "Role 1 bullet 2..."],
    ["Role 2 bullet 1...", ...]
  ],
  "change_rationale": ["Specific framing/keyword/adjacent-bridge decision 1...", ...]
}"""

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    if prior_draft and refinement_feedback:
        messages.append({"role": "assistant", "content": json.dumps(prior_draft)})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your previous draft failed automated preservation & quality checks:\n"
                    f"{refinement_feedback}\n\n"
                    "Fix EVERY issue above while keeping strong ATS alignment and return the full corrected JSON."
                ),
            }
        )

    resp = await post_with_retry(
        client,
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model_id,
            "messages": messages,
            "temperature": 0.2 if not refinement_feedback else 0.1,
            "response_format": {"type": "json_object"},
        },
        timeout=60.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Resume tailor LLM failed ({model_id}): {resp.status_code} {resp.text}")

    raw_data = json.loads(resp.json()["choices"][0]["message"]["content"])
    validated = TailoredResumeOutput(**raw_data).model_dump()

    # Synchronize role_1..3 from tailored_bullets_by_role for legacy consumers
    by_role = validated.get("tailored_bullets_by_role") or []
    if by_role:
        for i in range(min(3, len(by_role))):
            if not validated.get(f"tailored_bullets_role_{i + 1}"):
                validated[f"tailored_bullets_role_{i + 1}"] = by_role[i]
    else:
        assembled = [validated.get(f"tailored_bullets_role_{i}") or [] for i in range(1, 4)]
        assembled = [r for r in assembled if r]
        if assembled:
            validated["tailored_bullets_by_role"] = assembled

    return validated


async def verify_no_hallucination_with_jev(
    client: httpx.AsyncClient, tailored: dict, custom_instruction: str = ""
) -> float:
    """Uses Jev System-1 noul primitive to verify resume credibility. Fails loudly via AuditUnavailable."""
    cfg = load_config()
    url, api_key, model_id = resolve_triage_endpoint(cfg.models.triage.provider, cfg.models.triage.model)
    if not url or not api_key:
        raise AuditUnavailable("No API key configured for Jev hallucination audit.")

    profile = get_master_profile()

    payload = {
        "model": model_id,
        "state": {
            "verified_candidate_profile": {
                "allowed_skills": profile["core_skills"],
                "original_experience": profile["experience"],
            },
            "user_explicit_instruction": custom_instruction,
            "proposed_tailored_output": tailored,
        },
        "questions": {
            "has_hallucination": {
                "type": "noul",
                "instructions": "Does proposed_tailored_output fabricate false employers, fake degrees, fake metrics, or unrelated major platforms not present in the profile and not requested in user_explicit_instruction?",
                "criteria": {
                    "true": "Fabricates fake employers, fake degrees, invented metrics, or unrelated standalone platforms (e.g., iOS Swift, COBOL, Kernel C) absent from profile and not requested by user_explicit_instruction.",
                    "false": "Preserves verified projects and metrics, rephrases for JD alignment, bridges natural adjacent ecosystem terms (e.g., NumPy alongside Pandas, REST APIs for FastAPI), or obeys user_explicit_instruction.",
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
        raise AuditUnavailable(f"Malformed audit response: {e}") from e
    except AuditUnavailable:
        raise
    except Exception as e:
        raise AuditUnavailable(f"Audit request failed: {e}") from e


def render_typst_pdf(company: str, role_title: str, tailored: dict, job_id: int | None = None) -> str:
    """Compiles a clean, multi-page-safe, ATS-compliant PDF using Typst for arbitrary role counts."""
    profile = get_master_profile()
    output_dir = Path("output")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_company = re.sub(r"[^a-zA-Z0-9]+", "_", company or "Company").strip("_") or "Company"
    safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", str(profile.get("name") or "Candidate")).strip("_") or "Candidate"
    suffix = f"_{job_id}" if job_id is not None else ""
    typ_path = str(output_dir / f"resume_{safe_company}{suffix}.typ")
    pdf_path = str(output_dir / f"{safe_name}_{safe_company}{suffix}.pdf")

    experiences = profile.get("experience") or []
    if not isinstance(experiences, list) or len(experiences) == 0:
        raise ValueError(
            "Master profile must contain at least 1 experience entry to generate a tailored resume PDF."
        )

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
            s for s in profile.get("core_skills", []) if s not in tailored.get("matched_keywords", [])
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
        if not bullets:
            bullets = list(exp.get("bullets") or [])

        bullets_typ = "\n".join(f"- {escape_typst(b)}" for b in bullets)
        role_loc = exp.get("Location") or exp.get("location") or ""
        date_loc_parts = [p for p in (role_loc, exp.get("dates", "")) if p]
        right_meta = escape_typst(" | ".join(date_loc_parts))
        spacer = "\n#v(4pt)\n" if idx > 0 else ""
        # Keep role header together with first line cleanly across page breaks
        exp_blocks.append(
            f"""{spacer}#block(breakable: false)[#grid(columns: (1fr, auto),
  [*{escape_typst(exp.get('company', 'Company'))}* — _{escape_typst(exp.get('role', 'Role'))}_],
  [{right_meta}]
)]
{bullets_typ}"""
        )
    exp_section = "\n".join(exp_blocks)

    li_url = profile.get("linkedin", "")
    li_elem = (
        f'#link("{li_url if li_url.startswith("http") else "https://" + li_url}")[{escape_typst(li_url)}]'
        if li_url
        else ""
    )
    gh_url = profile.get("github", "")
    gh_elem = (
        f'#link("{gh_url if gh_url.startswith("http") else "https://" + gh_url}")[{escape_typst(gh_url)}]'
        if gh_url
        else ""
    )
    email = profile.get("email", "")
    email_elem = f'#link("mailto:{email}")[{escape_typst(email)}]' if email else ""

    contact_parts: list[str] = []
    if profile.get("location"):
        contact_parts.append(escape_typst(profile["location"]))
    if profile.get("phone"):
        contact_parts.append(escape_typst(profile["phone"]))
    if email_elem:
        contact_parts.append(email_elem)
    if li_elem:
        contact_parts.append(li_elem)
    if gh_elem:
        contact_parts.append(gh_elem)
    contact_line = " | ".join(contact_parts)

    educations = profile.get("education") or []
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
            "\n#v(5pt)\n#block(breakable: false)[#line(length: 100%, stroke: 0.5pt)\n"
            '#text(size: 10.5pt, weight: "bold")[EDUCATION] \\\n'
            + " \\\n".join(edu_lines)
            + "]"
        )

    typst_source = f"""#set page(paper: "a4", margin: (x: 1.3cm, y: 1.1cm))
#set text(size: 9.5pt)
#set par(justify: true, leading: 0.54em)

#align(center)[
  #text(size: 15pt, weight: "bold")[{escape_typst(profile.get('name', 'Candidate'))}] \\
  #v(2pt)
  #text(size: 8.5pt)[{contact_line}]
]

#v(4pt)
#line(length: 100%, stroke: 0.5pt)
#text(size: 10.5pt, weight: "bold")[EXECUTIVE SUMMARY] \\
{escape_typst(tailored.get('tailored_summary', ''))}

#v(5pt)
#line(length: 100%, stroke: 0.5pt)
#text(size: 10.5pt, weight: "bold")[EXPERIENCE]

{exp_section}

#v(5pt)
#line(length: 100%, stroke: 0.5pt)
#text(size: 10.5pt, weight: "bold")[TECHNICAL SKILLS]
{skills_block}{edu_section}
"""
    with open(typ_path, "w", encoding="utf-8") as f:
        f.write(typst_source)

    typst.compile(typ_path, output=pdf_path)
    return pdf_path


async def ensure_tailored_pdf(job_row: dict) -> str | None:
    """Regenerates PDF from stored tailored_resume_json with 0 LLM calls if output/ was wiped."""
    existing_path = job_row.get("tailored_pdf_path")
    if existing_path and Path(existing_path).exists():
        return existing_path

    tailored_json = job_row.get("tailored_resume_json")
    if not tailored_json:
        return existing_path if (existing_path and Path(existing_path).exists()) else None

    company = job_row.get("company", "Company")
    title = job_row.get("title", "Role")
    job_id = job_row.get("id")

    return await asyncio.to_thread(render_typst_pdf, company, title, tailored_json, job_id)


def compute_pdf_sha256(pdf_path: str) -> str:
    with open(pdf_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


async def tailor_resume_for_job(job: dict, custom_instruction: str = "") -> dict:
    """Runs LangGraph TailorGraph (Draft -> Preservation/ATS Verify -> Refine Loop -> Jev Audit -> Typst PDF)."""
    from src.orchestrator.graph import run_tailor_graph

    return await run_tailor_graph(job=job, custom_instruction=custom_instruction)
