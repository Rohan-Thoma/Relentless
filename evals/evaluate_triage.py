import math

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

def run_evaluation() -> str:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS total FROM jobs;")
        total_ingested = cur.fetchone()["total"]

        cur.execute("SELECT COUNT(*) AS passed FROM jobs WHERE triage_passed = TRUE;")
        total_passed = cur.fetchone()["passed"]

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
        msg = "⚠️ No ground-truth labels yet! Tap ✅ Apply or 👎 Skip on a few Telegram cards first."
        print(msg)
        return msg

    # Ground truth y = 1 if human approved (APPLIED), 0 if skipped
    y_true = [1 if r["human_action"] == "APPLIED" else 0 for r in labeled_rows]
    jev_probs = [float(r["jev_fit_prob"] or 0.0) for r in labeled_rows]
    base_probs = [float(r["cosine_sim"] or 0.0) for r in labeled_rows]

    total_applied = sum(y_true)
    callbacks = sum(1 for r in labeled_rows if r["outcome_status"] in ("CALLBACK", "INTERVIEW"))

    # 1. Overall Precision of Surfaced Cards + Wilson 95% CI
    overall_prec = total_applied / n
    ci_low, ci_high = wilson_95_ci(total_applied, n)

    # 2. Ablation: Ranking by Jev Fit Prob vs. Ranking by Baseline Skill Similarity
    jev_ranked_y = [
        1 if r["human_action"] == "APPLIED" else 0
        for r in sorted(labeled_rows, key=lambda x: float(x["jev_fit_prob"] or 0.0), reverse=True)
    ]
    base_ranked_y = [
        1 if r["human_action"] == "APPLIED" else 0
        for r in sorted(labeled_rows, key=lambda x: float(x["cosine_sim"] or 0.0), reverse=True)
    ]

    k_val = min(10, n)
    jev_p_k, jev_hits, k_eff = precision_at_k(jev_ranked_y, k_val)
    base_p_k, base_hits, _ = precision_at_k(base_ranked_y, k_val)
    jev_k_low, jev_k_high = wilson_95_ci(jev_hits, k_eff)
    base_k_low, base_k_high = wilson_95_ci(base_hits, k_eff)

    # 3. Calibration: Brier Score & Brier Skill Score (BSS)
    bs_jev = brier_score(jev_probs, y_true)
    bs_base = brier_score(base_probs, y_true)
    base_rate = total_applied / n
    bs_climatology = sum((base_rate - y) ** 2 for y in y_true) / n

    if bs_climatology > 0:
        bss_str = f"{1.0 - (bs_jev / bs_climatology):+.3f}"
    else:
        bss_str = "N/A (Click at least 1 'Skip' button on Telegram so both classes exist)"

    report = f"""# 📊 Relentless v1.0 — Live ML Evaluation & Calibration Report

* **Total Jobs Ingested:** `{total_ingested}` | **Cleared System-1 Gate:** `{total_passed}`
* **Human-Labeled Ground Truth ($n$):** `{n}` decisions (`{total_applied}` Applied, `{n - total_applied}` Skipped, `{callbacks}` Callbacks)
* **Overall Surfaced Approval Rate:** `{overall_prec:.1%}` *(95% Wilson CI: `[{ci_low:.1%}, {ci_high:.1%}]`, $n={n}$)*
* **Jev Brier Calibration Score:** `{bs_jev:.4f}` *(Lower is better)* | **Brier Skill Score (BSS):** `{bss_str}`

### 🔬 System-1 Ablation Study ($n = {n}$ Human Labels)

| Ranking / Triage Method | Precision@{k_eff} | 95% Wilson Confidence Interval | Brier Calibration Error |
|---|---|---|---|
| **Baseline (Lexical Keyword Cosine)** | `{base_p_k:.1%}` ({base_hits}/{k_eff}) | `[{base_k_low:.1%}, {base_k_high:.1%}]` | `{bs_base:.4f}` |
| **System-1 Jev (`jev-1.14` Rubric + Scam Gate)** | **`{jev_p_k:.1%}` ({jev_hits}/{k_eff})** | **`[{jev_k_low:.1%}, {jev_k_high:.1%}]`** | **`{bs_jev:.4f}`** |
"""
    print(report)
    return report

if __name__ == "__main__":
    run_evaluation()