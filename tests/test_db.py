from src.db import make_dedup_hash


def test_make_dedup_hash_stability():
    h1 = make_dedup_hash("Stripe", "Backend Engineer", "Remote")
    h2 = make_dedup_hash("Stripe", "Backend Engineer", "Remote")
    assert h1 == h2


def test_make_dedup_hash_normalization():
    h1 = make_dedup_hash("  Stripe  ", "Backend Engineer", "Remote")
    h2 = make_dedup_hash("stripe", "backend engineer", "remote")
    assert h1 == h2


def test_make_dedup_hash_differs():
    h1 = make_dedup_hash("Stripe", "Backend Engineer", "Remote")
    h2 = make_dedup_hash("Stripe", "Frontend Engineer", "Remote")
    assert h1 != h2