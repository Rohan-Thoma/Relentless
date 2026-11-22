from __future__ import annotations

import math
from typing import Any

from psycopg.types.json import Jsonb

from src.db import get_connection


def wilson_95_ci(successes: int, total: int) -> tuple[float, float]:
    """Computes the 95% Wilson Score Confidence Interval for small-n binomial proportions."""
    if total == 0:
        return (0.0, 0.0)
    z = 1.96
    phat = successes / total
    denom = 1.0 + (z * z) / total
    center = (phat + (z * z) / (2.0 * total)) / denom
    margin = (
        z
        * math.sqrt((phat * (1.0 - phat) / total) + ((z * z) / (4.0 * total * total)))
        / denom
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


def brier_score(probs: list[float], labels: list[int]) -> float:
    if not probs:
        return 0.0
    return sum((p - y) ** 2 for p, y in zip(probs, labels)) / len(probs)


def precision_at_k(sorted_labels: list[int], k: int) -> tuple[float, int, int]:
    if not sorted_labels:
        return (0.0, 0, 0)
    k_eff = min(k, len(sorted_labels))
    top_k = sorted_labels[:k_eff]
    hits = sum(top_k)
    return (hits / k_eff, hits, k_eff)


def compute_eval_snapshot() -> dict[str, Any]:
    """Computes structured eval metrics excluding non-learning skips (EXPIRED_OR_CLOSED)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS total FROM jobs;")
        total_ingested = int(cur.fetchone()["total"])

        cur.execute("SELECT COUNT(*) AS passed FROM jobs WHERE triage_passed = TRUE;")
        total_passed = int(cur.fetchone()["passed"])

        try:
            cur.execute(
                """
                SELECT id, company, title, cosine_sim, embedding_sim, jev_fit_prob, jev_fit_prob_cal,
                       jev_scam_prob, jev_fit_tier, human_action, outcome_status
                FROM jobs
                WHERE human_action IN ('APPLIED', 'SKIP_BAD_FIT', 'SKIP_SCAM')
                  AND COALESCE(learn_from_action, TRUE) = TRUE
                  AND COALESCE(skip_reason_category, '') <> 'EXPIRED_OR_CLOSED'
                ORDER BY human_action_at DESC;
                """
            )
            labeled_rows = cur.fetchall()
        except Exception:
            if hasattr(conn, "rollback"):
                conn.rollback()
            cur.execute(
                """
                SELECT id, company, title, cosine_sim, jev_fit_prob, jev_scam_prob,
                       jev_fit_tier, human_action, outcome_status
                FROM jobs
                WHERE human_action IN ('APPLIED', 'SKIP_BAD_FIT', 'SKIP_SCAM')
                ORDER BY human_action_at DESC;
                """
            )
            labeled_rows = cur.fetchall()

    n = len(labeled_rows)
    if n == 0:
        return {
            "total_ingested": total_ingested,
            "total_passed": total_passed,
            "n": 0,
            "total_applied": 0,
            "callbacks": 0,
            "overall_prec": 0.0,
            "ci": (0.0, 0.0),
            "k_eff": 0,
            "jev_p_k": 0.0,
            "jev_hits": 0,
            "jev_ci": (0.0, 0.0),
            "base_p_k": 0.0,
            "base_hits": 0,
            "base_ci": (0.0, 0.0),
            "bs_jev": 0.0,
            "bs_base": 0.0,
            "bss": None,
        }

    y_true = [1 if r["human_action"] == "APPLIED" else 0 for r in labeled_rows]
    jev_probs = [float(r["jev_fit_prob"] or 0.0) for r in labeled_rows]
    base_probs = [float(r.get("embedding_sim") or r.get("cosine_sim") or 0.0) for r in labeled_rows]

    total_applied = sum(y_true)
    callbacks = sum(1 for r in labeled_rows if r["outcome_status"] in ("CALLBACK", "INTERVIEW", "OFFER"))

    overall_prec = total_applied / n
    ci_low, ci_high = wilson_95_ci(total_applied, n)

    jev_ranked_y = [
        1 if r["human_action"] == "APPLIED" else 0
        for r in sorted(labeled_rows, key=lambda x: float(x["jev_fit_prob"] or 0.0), reverse=True)
    ]
    base_ranked_y = [
        1 if r["human_action"] == "APPLIED" else 0
        for r in sorted(
            labeled_rows,
            key=lambda x: float(x.get("embedding_sim") or x.get("cosine_sim") or 0.0),
            reverse=True,
        )
    ]

    k_val = min(10, n)
    jev_p_k, jev_hits, k_eff = precision_at_k(jev_ranked_y, k_val)
    base_p_k, base_hits, _ = precision_at_k(base_ranked_y, k_val)
    jev_k_low, jev_k_high = wilson_95_ci(jev_hits, k_eff)
    base_k_low, base_k_high = wilson_95_ci(base_hits, k_eff)

    bs_jev = brier_score(jev_probs, y_true)
    bs_base = brier_score(base_probs, y_true)
    base_rate = total_applied / n
    bs_climatology = sum((base_rate - y) ** 2 for y in y_true) / n
    bss = (1.0 - (bs_jev / bs_climatology)) if bs_climatology > 0 else None

    return {
        "total_ingested": total_ingested,
        "total_passed": total_passed,
        "n": n,
        "total_applied": total_applied,
        "callbacks": callbacks,
        "overall_prec": overall_prec,
        "ci": (ci_low, ci_high),
        "k_eff": k_eff,
        "jev_p_k": jev_p_k,
        "jev_hits": jev_hits,
        "jev_ci": (jev_k_low, jev_k_high),
        "base_p_k": base_p_k,
        "base_hits": base_hits,
        "base_ci": (base_k_low, base_k_high),
        "bs_jev": bs_jev,
        "bs_base": bs_base,
        "bss": bss,
    }


def record_eval_run(snap: dict[str, Any], config_version: str = "v4") -> int | None:
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO eval_runs (n, metrics, config_version) VALUES (%s, %s, %s) RETURNING id;",
                (
                    snap["n"],
                    Jsonb({
                        "bs_jev": snap["bs_jev"],
                        "bs_base": snap["bs_base"],
                        "jev_p_k": snap["jev_p_k"],
                        "base_p_k": snap["base_p_k"],
                        "bss": snap["bss"],
                        "overall_prec": snap["overall_prec"],
                    }),
                    config_version,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return int(row["id"]) if row else None
    except Exception:
        return None


def start_experiment(params: dict[str, Any], config_version: str = "v4") -> dict[str, Any]:
    """Starts a tracked A/B / before-after experiment in the `experiments` table."""
    snap = compute_eval_snapshot()
    before_id = record_eval_run(snap, config_version=config_version)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO experiments (params, status, eval_run_id_before)
            VALUES (%s, 'RUNNING', %s)
            RETURNING id, proposed_at, status, eval_run_id_before, params;
            """,
            (Jsonb(params or {}), before_id),
        )
        row = dict(cur.fetchone())
        conn.commit()
    return row


def complete_experiment(experiment_id: int, accept: bool | None = None, config_version: str = "v4") -> dict[str, Any]:
    """Completes a running experiment by recording eval_run_id_after and evaluating Precision@K/Brier lift."""
    snap = compute_eval_snapshot()
    after_id = record_eval_run(snap, config_version=config_version)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.eval_run_id_before, b.metrics AS before_metrics
            FROM experiments e
            LEFT JOIN eval_runs b ON b.id = e.eval_run_id_before
            WHERE e.id = %s;
            """,
            (experiment_id,),
        )
        exp = cur.fetchone()
        if not exp:
            raise ValueError(f"Experiment #{experiment_id} not found")
        before_m = exp.get("before_metrics") or {}
        if accept is None:
            # Auto-decide: accept if Precision@K didn't drop AND Brier improved or stayed equal
            improved = (snap["jev_p_k"] >= float(before_m.get("jev_p_k", 0.0))) and (
                snap["bs_jev"] <= float(before_m.get("bs_jev", 1.0)) + 1e-6
            )
            status = "ACCEPTED" if improved else "REJECTED"
        else:
            status = "ACCEPTED" if accept else "REJECTED"

        cur.execute(
            """
            UPDATE experiments
            SET status = %s,
                eval_run_id_after = %s,
                accepted_at = CASE WHEN %s = 'ACCEPTED' THEN NOW() ELSE accepted_at END
            WHERE id = %s
            RETURNING *;
            """,
            (status, after_id, status, experiment_id),
        )
        updated = dict(cur.fetchone())
        conn.commit()
    return updated


def list_experiments(limit: int = 20) -> list[dict[str, Any]]:
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.id, e.proposed_at, e.accepted_at, e.params, e.status,
                       e.eval_run_id_before, e.eval_run_id_after,
                       b.metrics AS before_metrics, a.metrics AS after_metrics
                FROM experiments e
                LEFT JOIN eval_runs b ON b.id = e.eval_run_id_before
                LEFT JOIN eval_runs a ON a.id = e.eval_run_id_after
                ORDER BY e.id DESC
                LIMIT %s;
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def run_evaluation() -> str:
    snap = compute_eval_snapshot()
    n = snap["n"]
    if n == 0:
        msg = "⚠️ No ground-truth labels yet! Tap ✅ Apply or 👎 Skip on a few Telegram/Web cards first."
        print(msg)
        return msg

    ci_low, ci_high = snap["ci"]
    jev_k_low, jev_k_high = snap["jev_ci"]
    base_k_low, base_k_high = snap["base_ci"]
    bss_str = (
        f"{snap['bss']:+.3f}"
        if snap["bss"] is not None
        else "N/A (Click at least 1 'Skip' button so both classes exist)"
    )

    report = f"""# 📊 Relentless v4 — Live ML Evaluation & Calibration Report

* **Total Jobs Ingested:** `{snap['total_ingested']}` | **Cleared System-1 Gate:** `{snap['total_passed']}`
* **Human-Labeled Ground Truth ($n$, excluding expired links):** `{n}` decisions (`{snap['total_applied']}` Applied, `{n - snap['total_applied']}` Skipped, `{snap['callbacks']}` Callbacks)
* **Overall Surfaced Approval Rate:** `{snap['overall_prec']:.1%}` *(95% Wilson CI: `[{ci_low:.1%}, {ci_high:.1%}]`, $n={n}$)*
* **Jev Brier Calibration Score:** `{snap['bs_jev']:.4f}` *(Lower is better)* | **Brier Skill Score (BSS):** `{bss_str}`

### 🔬 System-1 Ablation Study ($n = {n}$ Human Labels)

| Ranking / Triage Method | Precision@{snap['k_eff']} | 95% Wilson Confidence Interval | Brier Calibration Error |
|---|---|---|---|
| **Baseline (Semantic / Lexical Cosine)** | `{snap['base_p_k']:.1%}` ({snap['base_hits']}/{snap['k_eff']}) | `[{base_k_low:.1%}, {base_k_high:.1%}]` | `{snap['bs_base']:.4f}` |
| **System-1 Jev (`jev-1.14` Rubric + Scam Gate)** | **`{snap['jev_p_k']:.1%}` ({snap['jev_hits']}/{snap['k_eff']})** | **`[{jev_k_low:.1%}, {jev_k_high:.1%}]`** | **`{snap['bs_jev']:.4f}`** |
"""
    record_eval_run(snap, config_version="v4")
    print(report)
    return report


if __name__ == "__main__":
    run_evaluation()
