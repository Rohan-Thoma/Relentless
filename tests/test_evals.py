from evals.evaluate_triage import brier_score, precision_at_k, wilson_95_ci


def test_wilson_n0():
    assert wilson_95_ci(0, 0) == (0.0, 0.0)


def test_wilson_known():
    lo, hi = wilson_95_ci(5, 10)
    assert 0.0 < lo < hi < 1.0


def test_brier_score():
    assert brier_score([1.0, 0.0], [1, 0]) == 0.0
    assert brier_score([], []) == 0.0


def test_precision_at_k():
    assert precision_at_k([1, 1, 0, 0], 2) == (1.0, 2, 2)
    assert precision_at_k([], 5) == (0.0, 0, 0)