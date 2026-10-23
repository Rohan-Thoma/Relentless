import os
from fastmcp import FastMCP

from evals.evaluate_triage import run_evaluation
from src.db import get_connection
from src.apply.runner import prepare_application as _prepare_application
from src.tailor.resume_tailor import tailor_resume_for_job

mcp = FastMCP("Relentless-Career-Agent")


@mcp.tool()
def get_top_job_matches(limit: int = 5) -> list[dict]:
    """Returns the highest-scoring triaged jobs awaiting human review in Neon PostgreSQL."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT id, company, title, location, source_ats, jev_fit_prob,
                       jev_fit_tier, jev_scam_prob, jev_persona, apply_url
                FROM jobs
                WHERE triage_passed = TRUE AND human_action IS NULL
                ORDER BY jev_fit_prob DESC
                LIMIT %s;
                """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


@mcp.tool()
async def tailor_resume_pdf(job_id: int, custom_instruction: str = "") -> dict:
    """Runs Surgical ATS Resume Tailoring, Jev Hallucination Audit, and Typst PDF compilation for a job_id."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s;", (job_id,))
        job = cur.fetchone()
    if not job:
        return {"error": f"Job ID {job_id} not found."}
    return await tailor_resume_for_job(dict(job), custom_instruction=custom_instruction)


@mcp.tool()
def get_ml_evaluation_report() -> str:
    """Computes live Precision@K, Wilson 95% Confidence Intervals, and Brier Calibration metrics."""
    return run_evaluation()


@mcp.tool()
def get_job(job_id: int) -> dict:
    """Read a single job row by id."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s;", (job_id,))
        row = cur.fetchone()
    if not row:
        return {"error": f"Job ID {job_id} not found."}
    return dict(row)


@mcp.tool()
def get_apply_status(job_id: int) -> dict:
    """Return apply state and recent apply events for a job."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT apply_status, apply_state, apply_feasible, apply_blocker, tailored_pdf_path FROM jobs WHERE id = %s;", (job_id,))
        job = cur.fetchone()
        cur.execute("SELECT status, router_path, verification_status, started_at FROM apply_events WHERE job_id = %s ORDER BY started_at DESC LIMIT 10;", (job_id,))
        events = [dict(r) for r in cur.fetchall()]
    if not job:
        return {"error": f"Job ID {job_id} not found."}
    return {"job": dict(job), "events": events}


@mcp.tool()
def list_pending_confirmations() -> list[dict]:
    """List pending apply confirmations (read-only)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, job_id, kind, status, created_at, expires_at FROM apply_confirmations WHERE status = 'PENDING' ORDER BY created_at DESC;")
        return [dict(r) for r in cur.fetchall()]


@mcp.tool()
async def prepare_application(job_id: int) -> dict:
    """Prepare (prefill + evidence) an application. Never submits."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s;", (job_id,))
        job = cur.fetchone()
    if not job:
        return {"error": f"Job ID {job_id} not found."}
    return await _prepare_application(dict(job))


@mcp.tool()
def get_calibration_report() -> dict:
    """Return calibration model summary if an active model exists."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, n, method, metrics, active, fitted_at FROM calibration_models ORDER BY fitted_at DESC LIMIT 5;")
        rows = [dict(r) for r in cur.fetchall()]
    return {"models": rows}


@mcp.tool()
def list_experiments() -> list[dict]:
    """List recorded experiments."""
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM experiments ORDER BY id DESC LIMIT 50;")
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        return [{"error": str(e)}]


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    if transport == "http":
        api_key = os.getenv("MCP_API_KEY", "").strip()
        if not api_key:
            raise SystemExit("MCP_API_KEY is required when MCP_TRANSPORT=http")
        # FastMCP HTTP mode; auth enforced by deployment proxy / gateway in production.
        mcp.run(transport="http", host=os.getenv("MCP_HOST", "127.0.0.1"), port=int(os.getenv("MCP_PORT", "8000")))
    else:
        mcp.run()