from __future__ import annotations

import json
import logging
import os

from psycopg.types.json import Jsonb

from src.db import get_connection

logger = logging.getLogger(__name__)


def _pava(xs, ys):
    blocks = [[(x, y)] for x, y in zip(xs, ys)]

    def mean(b):
        return sum(y for _, y in b) / len(b)

    i = 0
    while i < len(blocks) - 1:
        if mean(blocks[i]) > mean(blocks[i + 1]):
            merged = blocks[i] + blocks[i + 1]
            blocks[i : i + 2] = [merged]
            if i > 0:
                i -= 1
        else:
            i += 1
    out = []
    for b in blocks:
        m = mean(b)
        for x, _ in b:
            out.append((x, m))
    return out


class CalibrationModel:
    def __init__(self, mapping: list[tuple[float, float]], n: int, method: str = "pava", metrics: dict | None = None):
        self.mapping = mapping
        self.n = n
        self.method = method
        self.metrics = metrics or {}

    def apply(self, p: float) -> float:
        chosen = self.mapping[0][1] if self.mapping else p
        for x, m in self.mapping:
            if p >= x:
                chosen = m
            else:
                break
        return max(0.0, min(1.0, chosen))

    def to_dict(self) -> dict:
        return {"n": self.n, "method": self.method, "mapping": self.mapping, "metrics": self.metrics}

    @classmethod
    def from_dict(cls, d: dict) -> CalibrationModel:
        return cls(
            mapping=[tuple(x) for x in d.get("mapping", [])],
            n=d.get("n", 0),
            method=d.get("method", "pava"),
            metrics=d.get("metrics") or {},
        )


def _brier(probs: list[float], labels: list[int]) -> float:
    if not probs:
        return 0.0
    return sum((p - y) ** 2 for p, y in zip(probs, labels)) / len(probs)


def evaluate_loocv_brier(pairs: list[tuple[float, int]]) -> tuple[float, float]:
    """Leave-one-out cross-validation comparing raw Brier vs PAVA calibrated Brier."""
    if len(pairs) < 4:
        return 1.0, 1.0
    raw_probs = [p for p, _ in pairs]
    ys = [y for _, y in pairs]
    raw_brier = _brier(raw_probs, ys)
    cal_preds: list[float] = []
    for idx, (p_val, _y_val) in enumerate(pairs):
        train = pairs[:idx] + pairs[idx + 1 :]
        train_sorted = sorted(train, key=lambda pair: pair[0])
        mapped = _pava([x for x, _ in train_sorted], [y for _, y in train_sorted])
        m = CalibrationModel(mapping=mapped, n=len(train))
        cal_preds.append(m.apply(p_val))
    cal_brier = _brier(cal_preds, ys)
    return round(raw_brier, 5), round(cal_brier, 5)


def fit_calibration(
    pairs: list[tuple[float, int]],
    min_n: int = 20,
    min_per_class: int = 0,
    require_brier_improvement: bool = False,
) -> CalibrationModel | None:
    if len(pairs) < min_n:
        return None
    pos = sum(1 for _, y in pairs if y == 1)
    neg = len(pairs) - pos
    if min_per_class > 0 and (pos < min_per_class or neg < min_per_class):
        return None

    metrics: dict = {"positives": pos, "negatives": neg}
    if require_brier_improvement:
        raw_b, cal_b = evaluate_loocv_brier(pairs)
        metrics["raw_brier_loocv"] = raw_b
        metrics["cal_brier_loocv"] = cal_b
        if cal_b >= raw_b:
            return None

    sorted_pairs = sorted(pairs, key=lambda p: p[0])
    xs = [p[0] for p in sorted_pairs]
    ys = [p[1] for p in sorted_pairs]
    mapped = _pava(xs, ys)
    return CalibrationModel(mapping=mapped, n=len(pairs), metrics=metrics)


def apply_calibration(model: CalibrationModel | None, p: float) -> float:
    if model is None:
        return p
    return model.apply(p)


def load_active_calibration_model() -> CalibrationModel | None:
    """Loads the active persisted calibration model from calibration_models table."""
    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
        return None
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT n, method, mapping, metrics
                FROM calibration_models
                WHERE active = TRUE
                ORDER BY fitted_at DESC
                LIMIT 1;
                """
            )
            row = cur.fetchone()
            if row and row.get("mapping"):
                return CalibrationModel(
                    mapping=[tuple(x) for x in row["mapping"]],
                    n=int(row.get("n") or 0),
                    method=str(row.get("method") or "pava"),
                    metrics=dict(row.get("metrics") or {}),
                )
    except Exception as e:
        logger.debug("Could not load active calibration model: %s", e)
    return None


def fit_and_persist_calibration_model(
    min_n: int = 30,
    min_per_class: int = 10,
    created_by: str = "system:auto",
) -> CalibrationModel | None:
    """Fits a balanced, LOOCV-validated calibration model and persists it to calibration_models.

    Excludes non-learning skips (EXPIRED_OR_CLOSED / learn_from_action = FALSE) so expired links
    never distort probability calibration.
    """
    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("RELENTLESS_TEST_LIVE_DB"):
        return None
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT jev_fit_prob, human_action
                FROM jobs
                WHERE human_action IN ('APPLIED', 'SKIP_BAD_FIT', 'SKIP_SCAM')
                  AND jev_fit_prob IS NOT NULL
                  AND COALESCE(learn_from_action, TRUE) = TRUE
                  AND COALESCE(skip_reason_category, '') <> 'EXPIRED_OR_CLOSED';
                """
            )
            rows = cur.fetchall()
            pairs = [
                (float(r["jev_fit_prob"]), 1 if r["human_action"] == "APPLIED" else 0)
                for r in rows
                if r.get("jev_fit_prob") is not None
            ]
            model = fit_calibration(
                pairs,
                min_n=min_n,
                min_per_class=min_per_class,
                require_brier_improvement=True,
            )
            if model is None:
                return None

            cur.execute("UPDATE calibration_models SET active = FALSE WHERE active = TRUE;")
            cur.execute(
                """
                INSERT INTO calibration_models (n, method, mapping, metrics, active, created_by)
                VALUES (%s, %s, %s, %s, TRUE, %s)
                RETURNING id;
                """,
                (
                    model.n,
                    model.method,
                    Jsonb(model.mapping),
                    Jsonb(model.metrics),
                    created_by,
                ),
            )
            conn.commit()
            return model
    except Exception as e:
        logger.debug("Calibration persistence skipped: %s", e)
        return None


def serialize(model: CalibrationModel | None) -> str | None:
    if model is None:
        return None
    return json.dumps(model.to_dict())


def deserialize(blob: str | None) -> CalibrationModel | None:
    if not blob:
        return None
    return CalibrationModel.from_dict(json.loads(blob))


def calibrate(pairs):
    model = fit_calibration(pairs)
    if model is None:
        return None
    return model.apply
