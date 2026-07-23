"""
golden_tests.py
===============
Hand-checkable golden rows. Each test recomputes a number from PRIMARY data by a
path independent of the engine, prints the arithmetic so a human can verify with
a calculator, and asserts the engine agrees. Also asserts the engineered
business stories are present so the dataset tells the intended story.

Run as a script for a readable report, or under pytest for pass/fail.
"""
from __future__ import annotations
import os
import sys
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import run_pipeline as rp
from calculations import driver_decomposition as dd

TOL = 0.01
T = rp.load()
OUT = rp.run(write=False)
LINE = OUT["variance_detail"]


def _row(month, dept, acct):
    r = LINE[(LINE["month"] == month) & (LINE["department_id"] == dept)
             & (LINE["account_id"] == acct)]
    assert len(r) == 1, f"expected 1 row, got {len(r)}"
    return r.iloc[0]


def test_golden_marketing_variance():
    """S&M Paid Marketing, 2025-09: hand-check the actual-vs-budget variance."""
    m, dept, acct = "2025-09-01", "SM", "SM_MKT"
    a = float(T["fact_actuals"].query("month==@m and department_id==@dept and account_id==@acct")["amount"].iloc[0])
    b = float(T["fact_budget"].query("month==@m and department_id==@dept and account_id==@acct")["amount"].iloc[0])
    expected_var = round(a - b, 2)
    expected_oi = round(-1 * expected_var, 2)  # expense: oi_sign = -1
    r = _row(m, dept, acct)
    print(f"\n[marketing] {m} S&M Paid Marketing")
    print(f"  actual={a:,.2f}  budget={b:,.2f}")
    print(f"  variance = actual - budget = {expected_var:,.2f}")
    print(f"  oi_impact = -1 * variance = {expected_oi:,.2f}  (unfavorable: spent more)")
    print(f"  engine: var={r['var_ab_amount']:,.2f} oi={r['oi_impact_ab']:,.2f} favorable={r['favorable_ab']}")
    assert abs(r["var_ab_amount"] - expected_var) <= TOL
    assert abs(r["oi_impact_ab"] - expected_oi) <= TOL
    assert bool(r["favorable_ab"]) is False  # overspend is unfavorable


def test_golden_revenue_favorable_logic():
    """Subscription revenue under budget must read UNFAVORABLE (not just negative)."""
    m = "2025-09-01"
    r = _row(m, "CORP", "REV_SUB")
    print(f"\n[revenue] {m} Subscription Revenue")
    print(f"  actual={r['actual']:,.2f} budget={r['budget']:,.2f} var={r['var_ab_amount']:,.2f}")
    print(f"  favorable={r['favorable_ab']} (revenue below budget => unfavorable)")
    assert r["actual"] < r["budget"]
    assert bool(r["favorable_ab"]) is False


def test_golden_salary_ties_to_headcount():
    """G&A salary, 2025-06: GL line == headcount x (avg_comp/12), exactly."""
    m, dept = "2025-06-01", "GA"
    hc = T["fact_headcount"].query("month==@m and department_id==@dept").iloc[0]
    expected = round(hc["actual_headcount"] * hc["avg_comp_actual"] / 12.0, 2)
    gl = float(T["fact_actuals"].query("month==@m and department_id==@dept and account_id=='GA_SAL'")["amount"].iloc[0])
    print(f"\n[salary] {m} G&A Salaries")
    print(f"  HC={hc['actual_headcount']}  avg_comp={hc['avg_comp_actual']:,.2f}/yr")
    print(f"  {hc['actual_headcount']} x {hc['avg_comp_actual']:,.2f} / 12 = {expected:,.2f}")
    print(f"  GL salary line = {gl:,.2f}")
    assert abs(gl - expected) <= TOL


def test_golden_comp_decomposition():
    """S&M comp decomposition, 2025-09: hc + rate impacts reconcile to variance."""
    m, dept = "2025-09-01", "SM"
    comp = dd.comp_decomposition(T["fact_headcount"])
    r = comp.query("month==@m and department_id==@dept").iloc[0]
    print(f"\n[comp decomp] {m} S&M")
    print(f"  salary_budget={r['salary_budget']:,.2f} salary_actual={r['salary_actual']:,.2f}")
    print(f"  salary_variance={r['salary_variance']:,.2f}")
    print(f"  hc_impact={r['hc_impact']:,.2f}  rate_impact={r['rate_impact']:,.2f}")
    print(f"  hc+rate={r['hc_impact']+r['rate_impact']:,.2f}  residual={r['decomp_residual']:,.2f}")
    assert abs(r["decomp_residual"]) <= TOL


def test_golden_arr_bridge():
    """ARR bridge identity, 2025-09."""
    m = "2025-09-01"
    s = T["fact_saas_metrics"].query("month==@m").iloc[0]
    calc = round(s["starting_arr"] + s["new_arr"] + s["expansion_arr"]
                 - s["contraction_arr"] - s["churned_arr"], 2)
    print(f"\n[arr bridge] {m}")
    print(f"  start={s['starting_arr']:,.0f} + new={s['new_arr']:,.0f} + exp={s['expansion_arr']:,.0f}"
          f" - con={s['contraction_arr']:,.0f} - chu={s['churned_arr']:,.0f}")
    print(f"  = {calc:,.2f}   ending_arr={s['ending_arr']:,.2f}")
    assert abs(calc - s["ending_arr"]) <= TOL


# ----- engineered business-story assertions (the data must tell the story) -----
def test_story_marketing_overspend():
    fy25 = LINE[(LINE["account_id"] == "SM_MKT") & (LINE["month"] >= "2025-01-01")]
    assert (fy25["var_ab_amount"] > 0).mean() > 0.8, "marketing should be over budget in FY2025"


def test_story_revenue_shortfall():
    fy25 = LINE[(LINE["account_id"] == "REV_SUB") & (LINE["month"] >= "2025-06-01")]
    assert (fy25["actual"] < fy25["budget"]).all(), "revenue should trail budget H2 FY2025"


def test_story_contractor_overrun():
    fy25 = LINE[(LINE["account_id"] == "RND_CONTRACT") & (LINE["month"] >= "2025-06-01")]
    assert (fy25["var_ab_amount"] > 0).all(), "R&D contractors should overrun in FY2025"


def test_story_legal_spike():
    spike = LINE[(LINE["account_id"] == "GA_LEGAL")
                 & (LINE["month"].isin(["2025-06-01", "2025-07-01"]))]
    assert (spike["var_ab_pct"] > 1.0).all(), "legal should spike >100% in Jun/Jul 2025"


def test_story_cs_under_hires_but_software_over():
    hc = rp.load()["fact_headcount"].query("department_id=='CS' and month>='2025-01-01'")
    assert (hc["actual_headcount"] < hc["budget_headcount"]).all(), "CS should be under plan on HC"
    sw = LINE[(LINE["account_id"] == "CS_SW") & (LINE["month"] >= "2025-01-01")]
    assert (sw["var_ab_amount"] > 0).mean() > 0.8, "CS software should be over budget"


def test_story_gross_margin_pressure():
    op = OUT["operating_metrics"]
    h1 = op[op["month"].between("2025-01-01", "2025-03-01")]["gross_margin"].mean()
    fy24 = op[op["month"] < "2025-01-01"]["gross_margin"].mean()
    assert h1 < fy24, "FY2025 gross margin should be pressured vs FY2024"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    print("=" * 70)
    print("GOLDEN TESTS  (hand-checkable arithmetic + story assertions)")
    print("=" * 70)
    for fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {fn.__name__}: {e}")
    print("\n" + "=" * 70)
    print(f"{passed}/{len(tests)} golden tests passed")
    print("=" * 70)
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(main())
