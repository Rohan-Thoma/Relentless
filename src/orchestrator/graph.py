from typing import TypedDict, Optional
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from langgraph.checkpoint.memory import MemorySaver
from src.db import get_connection
from src.tailor.resume_tailor import tailor_resume_for_job

class JobAgentState(TypedDict):
    job_id: int
    company: str
    title: str
    jd_text: str
    jev_persona: str
    human_decision: Optional[str]       # APPLIED | SKIP_BAD_FIT | SKIP_SCAM
    custom_edit_note: Optional[str]
    tailored_diff: Optional[str]
    pdf_path: Optional[str]

async def hitl_approval_node(state: JobAgentState) -> JobAgentState:
    """Pauses graph execution via LangGraph interrupt() until the human clicks a Telegram button."""
    if not state.get("human_decision"):
        decision_payload = interrupt(
            {
                "job_id": state["job_id"],
                "company": state["company"],
                "title": state["title"],
                "prompt": "Awaiting Telegram inline button click (APPLIED / SKIP_BAD_FIT / SKIP_SCAM)",
            }
        )
        state["human_decision"] = decision_payload.get("human_decision", "SKIP_BAD_FIT")
        state["custom_edit_note"] = decision_payload.get("custom_edit_note", "")
    return state

async def surgical_tailor_node(state: JobAgentState) -> JobAgentState:
    """Runs System-2 Resume Tailor + Jev Hallucination Audit + Typst PDF compile."""
    result = await tailor_resume_for_job(
        job={
            "company": state["company"],
            "title": state["title"],
            "jd_text": state["jd_text"],
            "jev_persona": state.get("jev_persona", "ai_agent_engineer"),
        },
        custom_instruction=state.get("custom_edit_note") or "",
    )
    state["tailored_diff"] = result["diff_summary"]
    state["pdf_path"] = result["pdf_path"]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET tailored_diff = %s WHERE id = %s;",
                (state["tailored_diff"], state["job_id"]),
            )
        conn.commit()
    return state

def route_after_hitl(state: JobAgentState) -> str:
    if state.get("human_decision") == "APPLIED":
        return "surgical_tailor"
    return END

def build_relentless_graph():
    builder = StateGraph(JobAgentState)
    builder.add_node("hitl_approval", hitl_approval_node)
    builder.add_node("surgical_tailor", surgical_tailor_node)

    builder.add_edge(START, "hitl_approval")
    builder.add_conditional_edges("hitl_approval", route_after_hitl, ["surgical_tailor", END])
    builder.add_edge("surgical_tailor", END)

    checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)

RELENTLESS_GRAPH = build_relentless_graph()