"""
adversarial_cases.py
====================
Generates adversarial narratives to prove the guardrails have teeth. Cases are
built RELATIVE to a real fact pack so the "fabricated" numbers are verified to be
genuinely outside the computed set at build time (not just assumed).

Case kinds
----------
clean            : a legitimate injection narrative -> must PASS.
fabricated_dollar: a $ figure with no computed value anywhere near -> must FAIL.
near_miss_dollar : a figure close to a real one but outside precision tolerance.
fabricated_pct   : a percentage not in the computed set -> must FAIL.
out_of_scope_ent : names a real account not in this pack -> entity audit FAILs.
"""
from __future__ import annotations
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from narrative import prompt as P
from guardrails import numeric_audit as na


def _is_fabricated_dollar(value: float, pack, lsd: float) -> bool:
    """True if no allowed dollar value is within the precision tolerance."""
    tol = max(0.5 * lsd, 1.0) * (1 + 1e-9)
    return not any(abs(value - a.value) <= tol for a in pack.allowed_by_kind("dollar"))


def _pick_fabricated_dollar(pack) -> float:
    """Find a 2-dp dollar value guaranteed outside the allowed set."""
    v = 1_240_137.53
    step = 111_311.17
    for _ in range(400):
        if _is_fabricated_dollar(v, pack, lsd=0.01):
            return round(v, 2)
        v += step
    return round(v, 2)


def make_cases(pack) -> list:
    """Build adversarial + control cases for one fact pack."""
    clean = P.injection_narrative(pack)
    cases = []

    # control: clean narrative must pass
    cases.append({"name": "clean_control", "text": clean, "check": "numeric",
                  "expect_caught": False})

    # fabricated dollar with no nearby computed value
    fab = _pick_fabricated_dollar(pack)
    cases.append({
        "name": "fabricated_dollar",
        "text": clean + f" Separately, we absorbed ${fab:,.2f} of one-time restructuring costs.",
        "check": "numeric", "expect_caught": True})

    # near-miss dollar: take a real driver variance and shift it well past tolerance
    drivers = pack.prompt_facts.get("top_drivers", [])
    if drivers:
        real = abs(drivers[0]["variance"])
        near = round(real * 1.9 + 12_345.67, 2)   # far enough to exceed tolerance
        if _is_fabricated_dollar(near, pack, lsd=0.01):
            cases.append({
                "name": "near_miss_dollar",
                "text": clean + f" The single largest overrun was actually ${near:,.2f}.",
                "check": "numeric", "expect_caught": True})

    # fabricated percentage not in the computed set
    fab_pct = 63.7
    if not any(abs(fab_pct/100 - a.value) <= 0.005 for a in pack.allowed_by_kind("percent")):
        cases.append({
            "name": "fabricated_pct",
            "text": clean + f" Net revenue retention deteriorated to {fab_pct}% this month.",
            "check": "numeric", "expect_caught": True})

    # coarse figures: a rounding window this wide cannot be verified, so the
    # audit must reject rather than bless. Without these the eval only tests
    # full-precision fabrications, which are the easy case.
    cases.append({
        "name": "coarse_dollar_1sf",
        "text": clean + " Total spend for the quarter was about $8M.",
        "check": "numeric", "expect_caught": True})
    cases.append({
        "name": "coarse_dollar_2sf",
        "text": clean + " The overrun came to roughly $3.4M for the period.",
        "check": "numeric", "expect_caught": True})

    # out-of-scope entity: a real account name deliberately not in this pack
    if "Insurance" not in pack.allowed_entities:
        cases.append({
            "name": "out_of_scope_entity",
            "text": clean + " Much of the pressure came from the Insurance line.",
            "check": "entity", "expect_caught": True})

    return cases
