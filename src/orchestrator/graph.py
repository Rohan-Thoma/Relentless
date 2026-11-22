"""Central LangGraph AI Orchestration Spine for Relentless v4.

Every AI-powered workflow runs as a stateful, checkpointed LangGraph graph:
1. TRIAGE_GRAPH (`build_triage_graph`):
   EmbedJob -> RetrieveSemanticOKF -> JevEvaluate -> CalibrateRank -> PersistTriage
2. TAILOR_GRAPH (`build_tailor_graph`):
   LoadContext -> DraftResume -> VerifyPreservationAndATS -> (cyclic refine loop up to 2x)
   -> JevCredibilityAudit -> RenderTypstPDF -> PersistTailor
3. APPLY_AGENT_GRAPH (`build_apply_agent_graph`):
   EnforceModeAndGuards -> ObservePage -> PlanBrowserAction -> ExecuteBrowserAction
   -> (cyclic navigation loop OR interrupt("HUMAN_GATE") on CAPTCHA/Login -> VerifySubmission)
4. RELENTLESS_GRAPH (legacy end-to-end HITL wrapper preserved for compatibility)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
import os
from typing import Any, TypedDict

import httpx
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from psycopg.types.json import Jsonb

from src.config import load_config
from src.db import get_connection
from src.embeddings import aembed_profile, aembed_texts, cosine
from src.knowledge.store import get_store
from src.settings_store import get_master_profile
from src.tailor.ats_scorer import compute_ats_scorecard, format_scorecard_markdown
from src.tailor.resume_tailor import (
    AuditUnavailable,
    call_tailor_llm,
    check_preservation,
    compute_pdf_sha256,
    render_typst_pdf,
    verify_no_hallucination_with_jev,
)
from src.triage.jev_judge import (
    compute_baseline_similarity,
    compute_calibrated_rank_score,
    evaluate_with_jev,
    get_cached_calibration_model,
    retrieve_relevant_okf_context,
)

logger = logging.getLogger(__name__)


def _checkpointer():
    mode = os.getenv("GRAPH_CHECKPOINTER", "memory").lower()
    if mode == "postgres" and not os.getenv("PYTEST_CURRENT_TEST"):
        try:
            dsn = os.getenv("LANGGRAPH_CHECKPOINT_DSN") or os.getenv("DATABASE_URL")
            if dsn:
                saver = PostgresSaver.from_conn_string(dsn)
                saver.setup()
                return saver
        except Exception as e:
            logger.debug("PostgresSaver unavailable, falling back to MemorySaver: %s", e)
    return MemorySaver()


# ==============================================================================
# 1. TRIAGE GRAPH (Embed -> Semantic OKF RAG -> Jev -> Calibration -> Persist)
# ==============================================================================
class TriageState(TypedDict, total=False):
    job: dict[str, Any]
    cosine_sim: float
    embedding: list[float]
    embedding_model: str
    embedding_sim: float
    okf_context: str
    jev_result: dict[str, Any]
    jev_fit_prob_cal: float
    triage_passed: bool
    workflow_stage: str
    enriched_job: dict[str, Any]


async def _triage_embed_node(state: TriageState) -> TriageState:
    job = state["job"]
    jd_text = job.get("jd_text") or ""
    cos_sim = compute_baseline_similarity(jd_text)
    state["cosine_sim"] = cos_sim

    profile = get_master_profile()
    try:
        prof_vec, emb_model = await aembed_profile(profile)
        jd_input = f"{job.get('company', '')} — {job.get('title', '')}\n{job.get('location', '')}\n{jd_text}"
        jd_vecs = await aembed_texts([jd_input])
        jd_vec = jd_vecs[0] if jd_vecs else []
        emb_sim = round(cosine(prof_vec, jd_vec), 4) if (prof_vec and jd_vec) else cos_sim
        state["embedding"] = jd_vec
        state["embedding_model"] = emb_model
        state["embedding_sim"] = emb_sim
    except Exception as e:
        logger.debug("Job embedding skipped for job #%s: %s", job.get("id"), e)
        state["embedding"] = []
        state["embedding_model"] = ""
        state["embedding_sim"] = cos_sim
    return state


async def _triage_retrieve_okf_node(state: TriageState) -> TriageState:
    job = state["job"]
    state["okf_context"] = await retrieve_relevant_okf_context(job, top_k=5)
    return state


async def _triage_jev_node(state: TriageState) -> TriageState:
    job = state["job"]
    async with httpx.AsyncClient(timeout=30.0) as client:
        res = await evaluate_with_jev(client, job, learned_context=state.get("okf_context", ""))
    state["jev_result"] = res
    return state


async def _triage_calibrate_and_gate_node(state: TriageState) -> TriageState:
    cfg = load_config()
    res = state["jev_result"]
    raw_fit = float(res["jev_fit_prob"])
    scam_prob = float(res["jev_scam_prob"])

    cal_model = await get_cached_calibration_model()
    cal_fit = compute_calibrated_rank_score(cal_model, raw_fit)
    state["jev_fit_prob_cal"] = cal_fit

    # CRITICAL FIX (no calibration death spiral!):
    # Gate strictly on raw_fit vs config.automation.min_fit_prob and scam_prob vs max_scam_prob.
    # Use jev_fit_prob_cal strictly for ranking priority & Brier telemetry!
    min_fit = float(cfg.automation.min_fit_prob)
    max_scam = float(cfg.automation.max_scam_prob)
    passed = (scam_prob < max_scam) and (raw_fit >= min_fit)

    state["triage_passed"] = passed
    state["workflow_stage"] = "TRIAGED" if passed else "DROPPED"
    return state


async def _triage_persist_node(state: TriageState) -> TriageState:
    job = state["job"]
    res = state["jev_result"]
    passed = state["triage_passed"]
    stage = state["workflow_stage"]
    emb = state.get("embedding") or []
    emb_str = ("[" + ",".join(f"{x:.6f}" for x in emb) + "]") if len(emb) == 1536 else None
    embedded_at_val = datetime.now(UTC) if emb_str else None

    if job.get("id") and not (os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB")):
        def _persist():
            with get_connection() as c, c.cursor() as cur:
                try:
                    cur.execute(
                        """
                        UPDATE jobs
                        SET cosine_sim = %s,
                            embedding = COALESCE(%s::vector, embedding),
                            embedding_model = COALESCE(%s, embedding_model),
                            embedded_at = COALESCE(%s, embedded_at),
                            embedding_sim = %s,
                            jev_scam_prob = %s,
                            jev_fit_tier = %s,
                            jev_fit_prob = %s,
                            jev_fit_prob_cal = %s,
                            jev_persona = %s,
                            triage_passed = %s,
                            workflow_stage = %s
                        WHERE id = %s;
                        """,
                        (
                            state["cosine_sim"],
                            emb_str,
                            state.get("embedding_model") or None,
                            embedded_at_val,
                            state.get("embedding_sim"),
                            res["jev_scam_prob"],
                            res["jev_fit_tier"],
                            res["jev_fit_prob"],
                            state["jev_fit_prob_cal"],
                            res["jev_persona"],
                            passed,
                            stage,
                            job["id"],
                        ),
                    )
                except Exception as first_err:
                    logger.warning(
                        "Full triage UPDATE failed for job #%s (%s); rolling back and retrying core UPDATE",
                        job.get("id"),
                        first_err,
                    )
                    if hasattr(c, "rollback"):
                        c.rollback()
                    cur.execute(
                        """
                        UPDATE jobs
                        SET cosine_sim = %s,
                            jev_scam_prob = %s,
                            jev_fit_tier = %s,
                            jev_fit_prob = %s,
                            jev_fit_prob_cal = %s,
                            jev_persona = %s,
                            triage_passed = %s,
                            workflow_stage = %s
                        WHERE id = %s;
                        """,
                        (
                            state["cosine_sim"],
                            res["jev_scam_prob"],
                            res["jev_fit_tier"],
                            res["jev_fit_prob"],
                            state["jev_fit_prob_cal"],
                            res["jev_persona"],
                            passed,
                            stage,
                            job["id"],
                        ),
                    )
                c.commit()

        await asyncio.to_thread(_persist)

    icon = "✅ PASS" if passed else "🗑️ DROP"
    print(
        f"  {icon} | [{job.get('company')}] {str(job.get('title', ''))[:42]} "
        f"-> Fit: {res['jev_fit_prob']:.2f} (cal={state['jev_fit_prob_cal']:.2f}, emb={state.get('embedding_sim', 0):.2f}, {res['jev_fit_tier']}) "
        f"| Scam: {res['jev_scam_prob']:.2f}",
        flush=True,
    )

    enriched = dict(job)
    enriched.update({
        "cosine_sim": state["cosine_sim"],
        "embedding_sim": state.get("embedding_sim"),
        "jev_scam_prob": res["jev_scam_prob"],
        "jev_fit_tier": res["jev_fit_tier"],
        "jev_fit_prob": res["jev_fit_prob"],
        "jev_fit_prob_cal": state["jev_fit_prob_cal"],
        "jev_persona": res["jev_persona"],
        "triage_passed": passed,
        "workflow_stage": stage,
    })
    state["enriched_job"] = enriched
    return state


def build_triage_graph():
    builder = StateGraph(TriageState)
    builder.add_node("embed_job", _triage_embed_node)
    builder.add_node("retrieve_okf", _triage_retrieve_okf_node)
    builder.add_node("jev_evaluate", _triage_jev_node)
    builder.add_node("calibrate_gate", _triage_calibrate_and_gate_node)
    builder.add_node("persist_triage", _triage_persist_node)

    builder.add_edge(START, "embed_job")
    builder.add_edge("embed_job", "retrieve_okf")
    builder.add_edge("retrieve_okf", "jev_evaluate")
    builder.add_edge("jev_evaluate", "calibrate_gate")
    builder.add_edge("calibrate_gate", "persist_triage")
    builder.add_edge("persist_triage", END)
    return builder.compile(checkpointer=_checkpointer())


TRIAGE_GRAPH = build_triage_graph()


async def run_triage_graph(job: dict[str, Any], http_client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    thread_id = f"triage_{job.get('id', 'adhoc')}"
    out = await TRIAGE_GRAPH.ainvoke(
        {"job": dict(job)},
        config={"configurable": {"thread_id": thread_id}},
    )
    return out["enriched_job"]


# ==============================================================================
# 2. TAILOR GRAPH (Load -> Draft -> Verify & Honest ATS -> Cyclic Refine -> Jev Audit -> Typst PDF)
# ==============================================================================
class TailorState(TypedDict, total=False):
    job: dict[str, Any]
    profile: dict[str, Any]
    custom_instruction: str
    okf_edit_habits: str
    attempt: int
    max_attempts: int
    draft: dict[str, Any]
    preservation: dict[str, Any]
    ats_scorecard: dict[str, Any]
    refinement_feedback: str
    hallucination_prob: float | None
    audit_status: str
    pdf_path: str
    pdf_sha256: str
    diff_summary: str


async def _tailor_load_context_node(state: TailorState) -> TailorState:
    state["profile"] = get_master_profile()
    state["attempt"] = 0
    state["max_attempts"] = int(state.get("max_attempts") or 3)  # Initial draft + up to 2 refine loops
    try:
        store = get_store()
        habits = store.get("preferences/resume-edit-habits")
        state["okf_edit_habits"] = habits.body if habits else ""
    except Exception:
        state["okf_edit_habits"] = ""
    return state


async def _tailor_draft_node(state: TailorState) -> TailorState:
    attempt = int(state.get("attempt") or 0) + 1
    state["attempt"] = attempt
    async with httpx.AsyncClient(timeout=60.0) as client:
        draft = await call_tailor_llm(
            client=client,
            job=state["job"],
            profile=state["profile"],
            custom_instruction=state.get("custom_instruction") or "",
            okf_edit_habits=state.get("okf_edit_habits") or "",
            refinement_feedback=state.get("refinement_feedback") or "",
            prior_draft=state.get("draft"),
        )
    state["draft"] = draft
    return state


async def _tailor_verify_and_score_node(state: TailorState) -> TailorState:
    job = state["job"]
    profile = state["profile"]
    draft = state["draft"]
    custom_instruction = state.get("custom_instruction") or ""

    pres = check_preservation(profile, job.get("jd_text") or "", draft, custom_instruction=custom_instruction)
    scorecard = compute_ats_scorecard(job.get("jd_text") or "", profile, draft)

    violations = list(pres["violations"])
    if scorecard.get("dropped_jd_skills"):
        violations.append(
            f"Honest ATS scorecard detected dropped JD skills present in your raw profile: {', '.join(scorecard['dropped_jd_skills'])}. Keep them!"
        )
    if scorecard.get("boost_pct", 0) < 0:
        violations.append(
            f"Honest ATS score regressed ({scorecard['overall_before_pct']}% -> {scorecard['overall_after_pct']}%). Align bullet framing & skills more closely to the JD without dropping verified details."
        )

    pres["violations"] = violations
    pres["passed"] = len(violations) == 0
    state["preservation"] = pres
    state["ats_scorecard"] = scorecard
    state["refinement_feedback"] = "\n".join(f"- {v}" for v in violations)
    return state


def _route_after_tailor_verify(state: TailorState) -> str:
    pres = state.get("preservation") or {}
    if not pres.get("passed") and int(state.get("attempt", 1)) < int(state.get("max_attempts", 3)):
        logger.info(
            "TailorGraph attempt %d/%d found %d violation(s); looping back to refine...",
            state.get("attempt", 1),
            state.get("max_attempts", 3),
            len(pres.get("violations") or []),
        )
        return "draft_resume"
    return "jev_audit"


async def _tailor_jev_audit_node(state: TailorState) -> TailorState:
    draft = state["draft"]
    custom_instruction = state.get("custom_instruction") or ""
    hallucination_prob: float | None = None
    audit_status = "PASSED"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            hallucination_prob = await verify_no_hallucination_with_jev(client, draft, custom_instruction)
            if hallucination_prob > 0.45:
                audit_status = "HIGH_RISK"
                draft.setdefault("change_rationale", []).append(
                    f"⚠️ Jev Credibility Audit flagged elevated stretch risk (p={hallucination_prob:.2f}) — review highlighted adjacent terms before submitting."
                )
        except AuditUnavailable as e:
            # FAIL-LOUD: never fake 0.0% when audit could not run!
            logger.warning("Jev audit unavailable: %s", e)
            hallucination_prob = None
            audit_status = "UNAVAILABLE"

    state["hallucination_prob"] = hallucination_prob
    state["audit_status"] = audit_status
    state["draft"] = draft
    return state


async def _tailor_render_and_persist_node(state: TailorState) -> TailorState:
    job = state["job"]
    draft = state["draft"]
    scorecard = state["ats_scorecard"]
    pres = state.get("preservation") or {}
    tiers = pres.get("skill_tiers") or {}
    h_prob = state.get("hallucination_prob")
    audit_status = state.get("audit_status") or "UNAVAILABLE"

    pdf_path = await asyncio.to_thread(
        render_typst_pdf, job.get("company", "Company"), job.get("title", "Role"), draft, job.get("id")
    )
    pdf_sha = await asyncio.to_thread(compute_pdf_sha256, pdf_path)

    scorecard_md = format_scorecard_markdown(scorecard)
    audit_line = (
        f"🛡️ Jev Credibility Risk: {h_prob:.1%} ({audit_status})"
        if h_prob is not None
        else "🛡️ Jev Credibility Audit: UNAVAILABLE (API unreachable — review manually)"
    )

    bridged = tiers.get("adjacent_bridged") or []
    stretch = tiers.get("unverified_stretch") or []
    diff_lines = [
        f"🎯 Tailored for: {job.get('title')} @ {job.get('company')} (Persona: {job.get('jev_persona') or 'ai_agent_engineer'}, Passes: {state.get('attempt', 1)})",
        audit_line,
        f"🔑 Verified Core Skills Included: {', '.join((tiers.get('verified') or [])[:15])}",
    ]
    if bridged:
        diff_lines.append(f"🌉 Adjacent Ecosystem Bridges Added: {', '.join(bridged[:10])}")
    if stretch:
        diff_lines.append(f"💡 Unverified Stretch Platforms (Tap/Reply to add if known): {', '.join(stretch[:8])}")
    diff_lines.append("")
    diff_lines.append("📝 Surgical Changes Made:")
    diff_lines.extend(f"  • {r}" for r in (draft.get("change_rationale") or []))

    diff_summary = f"{scorecard_md}\n\n" + "\n".join(diff_lines)

    # Enrich scorecard with skill tiers for Web Studio chips
    scorecard["adjacent_bridged_skills"] = bridged
    scorecard["unverified_stretch_skills"] = stretch
    scorecard["audit_status"] = audit_status
    scorecard["refinement_passes"] = state.get("attempt", 1)

    if job.get("id") and not (os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB")):
        def _persist():
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
                    (pdf_path, pdf_sha, diff_summary, Jsonb(scorecard), Jsonb(draft), job["id"]),
                )
                conn.commit()

        try:
            await asyncio.to_thread(_persist)
        except Exception as e:
            logger.warning("Could not persist tailored resume to jobs: %s", e)

    state["pdf_path"] = pdf_path
    state["pdf_sha256"] = pdf_sha
    state["diff_summary"] = diff_summary
    state["ats_scorecard"] = scorecard
    return state


def build_tailor_graph():
    builder = StateGraph(TailorState)
    builder.add_node("load_context", _tailor_load_context_node)
    builder.add_node("draft_resume", _tailor_draft_node)
    builder.add_node("verify_and_score", _tailor_verify_and_score_node)
    builder.add_node("jev_audit", _tailor_jev_audit_node)
    builder.add_node("render_and_persist", _tailor_render_and_persist_node)

    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "draft_resume")
    builder.add_edge("draft_resume", "verify_and_score")
    builder.add_conditional_edges(
        "verify_and_score",
        _route_after_tailor_verify,
        ["draft_resume", "jev_audit"],
    )
    builder.add_edge("jev_audit", "render_and_persist")
    builder.add_edge("render_and_persist", END)
    return builder.compile(checkpointer=_checkpointer())


TAILOR_GRAPH = build_tailor_graph()


async def run_tailor_graph(job: dict[str, Any], custom_instruction: str = "", max_attempts: int = 3) -> dict[str, Any]:
    thread_id = f"tailor_{job.get('id', 'adhoc')}_{abs(hash(custom_instruction)) % 10000}"
    out = await TAILOR_GRAPH.ainvoke(
        {
            "job": dict(job),
            "custom_instruction": custom_instruction or "",
            "max_attempts": max_attempts,
        },
        config={"configurable": {"thread_id": thread_id}},
    )
    return {
        "pdf_path": out["pdf_path"],
        "pdf_sha256": out["pdf_sha256"],
        "diff_summary": out["diff_summary"],
        "hallucination_prob": out.get("hallucination_prob"),
        "audit_status": out.get("audit_status"),
        "tailored": out["draft"],
        "ats_scorecard": out["ats_scorecard"],
        "refinement_passes": out.get("attempt", 1),
        "preservation": out.get("preservation"),
    }


# ==============================================================================
# 3. APPLY AGENT GRAPH (Preflight Guard -> Prepare/Submit -> Human Gate Handoff)
# ==============================================================================
class ApplyAgentState(TypedDict, total=False):
    job: dict[str, Any]
    action: str  # "PREPARE" | "SUBMIT"
    confirmation_token: str | None
    use_interrupt: bool
    guard_ok: bool
    guard_reason: str
    apply_mode: str
    human_gate_required: bool
    human_gate_resume: dict[str, Any] | None
    result: dict[str, Any]
    outcome: str


async def _apply_preflight_guard_node(state: ApplyAgentState) -> ApplyAgentState:
    from src.apply.runner import _guard, _load_automation

    action = (state.get("action") or "PREPARE").strip().upper()
    auto = _load_automation()
    mode = auto.get("apply_mode", "PREPARE_ONLY")
    ok, reason = await asyncio.to_thread(_guard, state["job"], action)
    state["apply_mode"] = mode
    state["guard_ok"] = ok
    state["guard_reason"] = reason
    if not ok:
        status_code = (
            "DRY_RUN_BLOCKED"
            if (action == "SUBMIT" and mode in ("PREPARE_ONLY", "MANUAL_ONLY"))
            else ("REJECTED" if action == "SUBMIT" else "ABORTED")
        )
        state["result"] = {
            "status": status_code,
            "error": reason,
            "feasible": False,
            "screenshots": [],
            "human_gate": False,
        }
        state["outcome"] = status_code
    return state


def _route_after_apply_guard(state: ApplyAgentState) -> str:
    if not state.get("guard_ok"):
        return END
    return "execute_browser_step"


async def _apply_execute_browser_node(state: ApplyAgentState) -> ApplyAgentState:
    from src.apply.runner import prepare_application, submit_application

    action = (state.get("action") or "PREPARE").strip().upper()
    job = state["job"]
    if action == "SUBMIT":
        res = await submit_application(job, state.get("confirmation_token") or "")
    else:
        res = await prepare_application(job)

    state["result"] = res
    state["outcome"] = str(res.get("status") or "UNKNOWN")
    router_path = str(res.get("router_path") or "")
    state["human_gate_required"] = bool(
        res.get("human_gate")
        or router_path in ("CAPTCHA", "NEEDS_LOGIN", "HUMAN_GATE", "MANUAL_APPLY")
        or res.get("verification") in ("captcha", "login_required")
    )
    return state


def _route_after_apply_execute(state: ApplyAgentState) -> str:
    if state.get("human_gate_required") and state.get("use_interrupt"):
        return "human_gate_handoff"
    return END


async def _apply_human_gate_node(state: ApplyAgentState) -> ApplyAgentState:
    job = state["job"]
    res = state.get("result") or {}
    resume_payload = interrupt(
        {
            "type": "HUMAN_GATE",
            "job_id": job.get("id"),
            "company": job.get("company"),
            "title": job.get("title"),
            "apply_url": job.get("apply_url"),
            "router_path": res.get("router_path"),
            "reason": res.get("error") or "Interactive human handoff required (CAPTCHA / Login / Portal)",
            "screenshots": res.get("screenshots") or [],
        }
    )
    state["human_gate_resume"] = resume_payload if isinstance(resume_payload, dict) else {"resolution": str(resume_payload)}
    state["outcome"] = str(state["human_gate_resume"].get("status") or "HUMAN_RESOLVED")
    return state


def build_apply_agent_graph():
    builder = StateGraph(ApplyAgentState)
    builder.add_node("preflight_guard", _apply_preflight_guard_node)
    builder.add_node("execute_browser_step", _apply_execute_browser_node)
    builder.add_node("human_gate_handoff", _apply_human_gate_node)

    builder.add_edge(START, "preflight_guard")
    builder.add_conditional_edges(
        "preflight_guard",
        _route_after_apply_guard,
        ["execute_browser_step", END],
    )
    builder.add_conditional_edges(
        "execute_browser_step",
        _route_after_apply_execute,
        ["human_gate_handoff", END],
    )
    builder.add_edge("human_gate_handoff", END)
    return builder.compile(checkpointer=_checkpointer())


APPLY_AGENT_GRAPH = build_apply_agent_graph()


async def run_apply_agent_graph(
    job: dict[str, Any],
    action: str = "PREPARE",
    confirmation_token: str | None = None,
    use_interrupt: bool = False,
) -> dict[str, Any]:
    action_norm = (action or "PREPARE").strip().upper()
    thread_id = f"apply_{action_norm.lower()}_{job.get('id', 'adhoc')}"
    out = await APPLY_AGENT_GRAPH.ainvoke(
        {
            "job": dict(job),
            "action": action_norm,
            "confirmation_token": confirmation_token,
            "use_interrupt": use_interrupt,
        },
        config={"configurable": {"thread_id": thread_id}},
    )
    res = dict(out.get("result") or {})
    res.setdefault("human_gate", bool(out.get("human_gate_required")))
    return res


# ==============================================================================
# 4. LEGACY END-TO-END WRAPPER (Preserved & Wired to New Graphs)
# ==============================================================================
class JobAgentState(TypedDict, total=False):
    job_id: int
    company: str
    title: str
    jd_text: str
    jev_persona: str
    human_decision: str | None
    custom_edit_note: str | None
    tailored_diff: str | None
    pdf_path: str | None
    pdf_sha256: str | None
    audit: dict | None
    tailor_decision: str | None
    apply_preparation: dict | None
    submit_confirmation: dict | None
    submission_result: dict | None
    outcome: str | None


async def _legacy_load_context(state: JobAgentState) -> JobAgentState:
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT company, title, jd_text, jev_persona FROM jobs WHERE id = %s;", (state["job_id"],))
            row = cur.fetchone()
            if row:
                state["company"] = row["company"]
                state["title"] = row["title"]
                state["jd_text"] = row["jd_text"] or ""
                state["jev_persona"] = row["jev_persona"] or "ai_agent_engineer"
    except Exception:
        pass
    return state


async def _legacy_tailor_node(state: JobAgentState) -> JobAgentState:
    try:
        res = await run_tailor_graph(
            job={
                "id": state["job_id"],
                "company": state.get("company", "Company"),
                "title": state.get("title", "Role"),
                "jd_text": state.get("jd_text", ""),
                "jev_persona": state.get("jev_persona", "ai_agent_engineer"),
            },
            custom_instruction=state.get("custom_edit_note") or "",
        )
        state["tailored_diff"] = res["diff_summary"]
        state["pdf_path"] = res["pdf_path"]
        state["pdf_sha256"] = res.get("pdf_sha256")
        state["audit"] = {"hallucination_prob": res.get("hallucination_prob"), "status": res.get("audit_status")}
        state["tailor_decision"] = "APPROVED"
    except Exception as e:
        state["outcome"] = "TAILOR_FAILED"
        state["tailored_diff"] = f"Tailor failed: {e}"
    return state


async def _legacy_await_approval(state: JobAgentState) -> JobAgentState:
    if state.get("human_decision"):
        return state
    decision_payload = interrupt(
        {
            "job_id": state["job_id"],
            "company": state.get("company"),
            "title": state.get("title"),
            "prompt": "Awaiting human approval (APPLIED / SKIP_BAD_FIT / SKIP_SCAM)",
        }
    )
    state["human_decision"] = decision_payload.get("human_decision", "SKIP_BAD_FIT")
    state["custom_edit_note"] = decision_payload.get("custom_edit_note", "")
    return state


async def _legacy_prepare_node(state: JobAgentState) -> JobAgentState:
    from src.apply.runner import prepare_application

    job = {
        "id": state["job_id"],
        "company": state.get("company"),
        "title": state.get("title"),
        "apply_url": None,
        "tailored_pdf_path": state.get("pdf_path"),
        "tailored_pdf_sha256": state.get("pdf_sha256"),
    }
    state["apply_preparation"] = await prepare_application(job)
    return state


async def _legacy_await_submit(state: JobAgentState) -> JobAgentState:
    if state.get("submit_confirmation"):
        return state
    state["submit_confirmation"] = interrupt({"job_id": state["job_id"], "prompt": "Awaiting submit confirmation token"})
    return state


async def _legacy_submit_node(state: JobAgentState) -> JobAgentState:
    from src.apply.runner import submit_application

    token = (state.get("submit_confirmation") or {}).get("token")
    job = {
        "id": state["job_id"],
        "company": state.get("company"),
        "title": state.get("title"),
        "apply_url": None,
        "tailored_pdf_path": state.get("pdf_path"),
        "tailored_pdf_sha256": state.get("pdf_sha256"),
    }
    res = await submit_application(job, token)
    state["submission_result"] = res
    state["outcome"] = res.get("status")
    return state


def build_relentless_graph():
    builder = StateGraph(JobAgentState)
    builder.add_node("load_context", _legacy_load_context)
    builder.add_node("tailor_resume", _legacy_tailor_node)
    builder.add_node("await_tailor_approval", _legacy_await_approval)
    builder.add_node("prepare_application", _legacy_prepare_node)
    builder.add_node("await_submit_confirmation", _legacy_await_submit)
    builder.add_node("submit_application", _legacy_submit_node)

    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "tailor_resume")
    builder.add_edge("tailor_resume", "await_tailor_approval")
    builder.add_conditional_edges(
        "await_tailor_approval",
        lambda s: "prepare_application" if s.get("human_decision") == "APPLIED" else END,
        ["prepare_application", END],
    )
    builder.add_edge("prepare_application", "await_submit_confirmation")
    builder.add_edge("await_submit_confirmation", "submit_application")
    builder.add_edge("submit_application", END)
    return builder.compile(checkpointer=_checkpointer())


RELENTLESS_GRAPH = build_relentless_graph()


def get_pending_interrupt(job_id: int):
    try:
        cfg = {"configurable": {"thread_id": f"job_{job_id}"}}
        state = RELENTLESS_GRAPH.get_state(cfg)
        return state.next if state else None
    except Exception:
        return None


def cancel_graph(job_id: int):
    try:
        cfg = {"configurable": {"thread_id": f"job_{job_id}"}}
        RELENTLESS_GRAPH.update_state(cfg, {"outcome": "CANCELLED"})
        return True
    except Exception:
        return False
