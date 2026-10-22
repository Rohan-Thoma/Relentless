from fastmcp import FastMCP

from evals.evaluate_triage import run_evaluation
from src.db import get_connection
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

if __name__ == "__main__":
    mcp.run()