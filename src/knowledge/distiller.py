import re
from src.knowledge.store import get_store, Concept, process_actor
from src.db import get_connection

REDACT = True


def _redact(text: str) -> str:
    if not REDACT:
        return text
    text = re.sub(r"\b[\w\.-]+@[\w\.-]+\.\w+\b", "[email]", text)
    text = re.sub(r"\+?\d[\d\s-]{8,}\d", "[phone]", text)
    return text


def run_distill():
    store = get_store()
    store.hydrate()
    # pull recently labeled jobs
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, company, title, human_action FROM jobs WHERE human_action IS NOT NULL ORDER BY human_action_at DESC LIMIT 50;")
                rows = cur.fetchall()
    except Exception:
        rows = []
    forgotten = set()
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pattern FROM knowledge_forget;")
                forgotten = {r["pattern"] for r in cur.fetchall()}
    except Exception:
        pass
    for row in rows:
        company = row["company"]
        if any(f in company for f in forgotten):
            continue
        if row["human_action"] == "SKIP_SCAM":
            path = f"patterns/scam-{company.lower().replace(' ', '-')}"
            body = _redact(f"Scam pattern derived from job {row['id']} at {company}.")
            concept = Concept(path=path, concept_type="Scam Pattern", name=f"Scam: {company}", body=body, status="draft", tags=["scam"])
            store.upsert(concept, actor=process_actor("distiller"))
    store.flush()
    return True