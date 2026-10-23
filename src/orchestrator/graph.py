import os
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from src.db import get_connection
from src.tailor.resume_tailor import tailor_resume_for_job, AuditUnavailable


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


def _checkpointer():
    mode = os.getenv("GRAPH_CHECKPOINTER", "memory").lower()
    if mode == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver

            dsn = os.getenv("LANGGRAPH_CHECKPOINT_DSN") or os.getenv("DATABASE_URL")
            saver = PostgresSaver.from_conn_string(dsn)
            saver.setup()
            return saver
        except Exception:
            pass
    return MemorySaver()


async def load_context(state: JobAgentState) -> JobAgentState:
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


async def tailor_resume(state: JobAgentState) -> JobAgentState:
    try:
        result = await tailor_resume_for_job(
            job={
                "company": state["company"],
                "title": state["title"],
                "jd_text": state.get("jd_text", ""),
                "jev_persona": state.get("jev_persona", "ai_agent_engineer"),
            },
            custom_instruction=state.get("custom_edit_note") or "",
        )
        state["tailored_diff"] = result["diff_summary"]
        state["pdf_path"] = result["pdf_path"]
        state["pdf_sha256"] = result.get("pdf_sha256")
        state["audit"] = {"hallucination_prob": result.get("hallucination_prob")}
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE jobs SET tailored_diff = %s, tailored_pdf_path = %s WHERE id = %s;",
                            (state["tailored_diff"], state["pdf_path"], state["job_id"]))
            conn.commit()
    except AuditUnavailable as e:
        state["outcome"] = "AUDIT_UNAVAILABLE"
        state["tailored_diff"] = f"Audit unavailable: {e}"
    except Exception as e:
        state["outcome"] = "TAILOR_FAILED"
        state["tailored_diff"] = f"Tailor failed: {e}"
    return state


async def audit_resume(state: JobAgentState) -> JobAgentState:
    # audit already performed inside tailor; mark
    if state.get("outcome") in ("AUDIT_UNAVAILABLE", "TAILOR_FAILED"):
        return state
    state["tailor_decision"] = "APPROVED"
    return state


async def await_tailor_approval(state: JobAgentState) -> JobAgentState:
    if state.get("human_decision"):
        return state
    decision_payload = interrupt(
        {
            "job_id": state["job_id"],
            "company": state.get("company"),
            "title": state.get("title"),
            "prompt": "Awaiting Telegram inline button click (APPLIED / SKIP_BAD_FIT / SKIP_SCAM)",
        }
    )
    state["human_decision"] = decision_payload.get("human_decision", "SKIP_BAD_FIT")
    state["custom_edit_note"] = decision_payload.get("custom_edit_note", "")
    return state


async def prepare_application_node(state: JobAgentState) -> JobAgentState:
    from src.apply.runner import prepare_application

    job = {"id": state["job_id"], "company": state.get("company"), "title": state.get("title"),
           "apply_url": None, "jev_fit_prob": 0.9, "jev_fit_tier": "strong_fit",
           "tailored_pdf_path": state.get("pdf_path"), "tailored_pdf_sha256": state.get("pdf_sha256")}
    # fetch apply_url
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT apply_url FROM jobs WHERE id = %s;", (state["job_id"],))
            row = cur.fetchone()
            if row:
                job["apply_url"] = row["apply_url"]
    except Exception:
        pass
    res = await prepare_application(job)
    state["apply_preparation"] = res
    return state


async def await_submit_confirmation(state: JobAgentState) -> JobAgentState:
    if state.get("submit_confirmation"):
        return state
    decision_payload = interrupt(
        {
            "job_id": state["job_id"],
            "prompt": "Awaiting submit confirmation token",
        }
    )
    state["submit_confirmation"] = decision_payload
    return state


async def submit_application_node(state: JobAgentState) -> JobAgentState:
    from src.apply.runner import submit_application

    token = (state.get("submit_confirmation") or {}).get("token")
    job = {"id": state["job_id"], "company": state.get("company"), "title": state.get("title"),
           "apply_url": None, "tailored_pdf_path": state.get("pdf_path"),
           "tailored_pdf_sha256": state.get("pdf_sha256")}
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT apply_url FROM jobs WHERE id = %s;", (state["job_id"],))
            row = cur.fetchone()
            if row:
                job["apply_url"] = row["apply_url"]
    except Exception:
        pass
    res = await submit_application(job, token)
    state["submission_result"] = res
    state["outcome"] = res.get("status")
    return state


async def record_result(state: JobAgentState) -> JobAgentState:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE jobs SET outcome_status = %s WHERE id = %s;",
                            (state.get("outcome") or "PENDING", state["job_id"]))
            conn.commit()
    except Exception:
        pass
    return state


def route_after_hitl(state: JobAgentState) -> str:
    if state.get("human_decision") == "APPLIED":
        return "tailor_resume"
    return "record_result"


def route_after_tailor(state: JobAgentState) -> str:
    if state.get("outcome") in ("AUDIT_UNAVAILABLE", "TAILOR_FAILED"):
        return "record_result"
    return "audit_resume"


def route_after_audit(state: JobAgentState) -> str:
    return "await_tailor_approval"


def build_relentless_graph():
    builder = StateGraph(JobAgentState)
    builder.add_node("load_context", load_context)
    builder.add_node("tailor_resume", tailor_resume)
    builder.add_node("audit_resume", audit_resume)
    builder.add_node("await_tailor_approval", await_tailor_approval)
    builder.add_node("prepare_application", prepare_application_node)
    builder.add_node("await_submit_confirmation", await_submit_confirmation)
    builder.add_node("submit_application", submit_application_node)
    builder.add_node("record_result", record_result)

    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "tailor_resume")
    builder.add_conditional_edges("tailor_resume", route_after_tailor, ["audit_resume", "record_result"])
    builder.add_edge("audit_resume", "await_tailor_approval")
    builder.add_conditional_edges("await_tailor_approval", route_after_hitl, ["tailor_resume", "record_result"])
    # Note: tailor_approval loop; for simplicity end after first
    builder.add_edge("prepare_application", "await_submit_confirmation")
    builder.add_edge("await_submit_confirmation", "submit_application")
    builder.add_edge("submit_application", "record_result")
    builder.add_edge("record_result", END)

    checkpointer = _checkpointer()
    return builder.compile(checkpointer=checkpointer)


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