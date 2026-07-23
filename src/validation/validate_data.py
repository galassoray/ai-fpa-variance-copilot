"""
validate_data.py
================
Proves the deterministic layer is internally consistent BEFORE any UI or LLM is
built. Each check returns PASS/FAIL with the worst observed discrepancy. Results
are written to outputs/validation_results.csv and printed.

Checks
------
1  ARR bridge ties          : start + new + exp - con - chu == ending
2  Variance identity        : budget + var_ab_amount == actual (line grain)
3  Revenue fav/unfav flag    : revenue favorable iff actual > budget
4  Expense fav/unfav flag    : expense favorable iff actual < budget
5  % variance safety         : no inf / no NaN where budget >= floor
6  Comp decomposition        : hc_impact + rate_impact == salary_variance
7  Revenue decomposition     : volume + price == rev_variance
8  Referential integrity     : every fact key exists in its dimension
9  Salary ties to headcount  : GL salary == headcount x (avg_comp/12)
10 Rollup ties to lines      : company variance == sum of line variances
11 Operating income identity : OI == revenue - cogs - opex
"""
from __future__ import annotations
import os
import sys
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import duckdb
from calculations import driver_decomposition as dd
from calculations import saas_metrics as sm
import run_pipeline as rp

OUT = os.path.join(HERE, "..", "..", "outputs")
DB = os.path.join(HERE, "..", "..", "data", "processed", "fpa.duckdb")
TOL = 0.01


def _result(name, passed, worst, detail=""):
    return {"check": name, "status": "PASS" if passed else "FAIL",
            "worst_discrepancy": round(float(worst), 4), "detail": detail}


def run_checks(t: dict | None = None, out: dict | None = None):
    """Run all 11 reconciliation checks.

    Optionally against a supplied scenario's tables/outputs rather than the
    canonical dataset -- so the guarantees can be asserted for ANY set of
    inputs, which is what makes live scenario editing safe.
    """
    if t is None:
        t = rp.load()
    if out is None:
        out = rp.compute(t)
    acct = t["dim_account"]
    res = []

    # 1 ARR bridge
    chk = sm.arr_bridge_check(t["fact_saas_metrics"])
    worst = chk["bridge_diff"].abs().max()
    res.append(_result("1_arr_bridge_ties", worst <= TOL, worst, "ending == start+new+exp-con-chu"))

    # 2 variance identity
    line = out["variance_detail"]
    diff = (line["budget"] + line["var_ab_amount"] - line["actual"]).abs().max()
    res.append(_result("2_variance_identity", diff <= TOL, diff, "budget + variance == actual"))

    # 3 revenue favorable flag
    rev = line[line["is_revenue"] == 1].copy()
    expect = rev["actual"] > rev["budget"]
    bad = (rev["favorable_ab"] != expect).sum()
    res.append(_result("3_revenue_fav_flag", bad == 0, bad, "revenue favorable iff actual>budget"))

    # 4 expense favorable flag
    exp = line[line["is_expense"] == 1].copy()
    expect = exp["actual"] < exp["budget"]
    bad = (exp["favorable_ab"] != expect).sum()
    res.append(_result("4_expense_fav_flag", bad == 0, bad, "expense favorable iff actual<budget"))

    # 5 pct safety
    sub = line[line["budget"].abs() >= 100]
    inf_ct = np.isinf(sub["var_ab_pct"].to_numpy(dtype="float64")).sum()
    nan_ct = sub["var_ab_pct"].isna().sum()
    res.append(_result("5_pct_variance_safety", inf_ct == 0 and nan_ct == 0,
                        inf_ct + nan_ct, "no inf/NaN where budget>=floor"))

    # 6 comp decomposition
    comp = dd.comp_decomposition(t["fact_headcount"])
    worst = comp["decomp_residual"].abs().max()
    res.append(_result("6_comp_decomposition_reconciles", worst <= TOL, worst,
                        "hc_impact + rate_impact == salary_variance"))

    # 7 revenue decomposition
    rdec = dd.revenue_decomposition(t["fact_saas_metrics"], t["fact_saas_metrics_budget"])
    worst = rdec["decomp_residual"].abs().max()
    res.append(_result("7_revenue_decomposition_reconciles", worst <= TOL, worst,
                        "volume + price == rev_variance"))

    # 8 referential integrity
    bad = 0
    dept_ids = set(t["dim_department"]["department_id"])
    acct_ids = set(t["dim_account"]["account_id"])
    month_ids = set(t["dim_date"]["month"].astype(str))
    for f in ["fact_actuals", "fact_budget", "fact_forecast"]:
        df = t[f]
        bad += (~df["department_id"].isin(dept_ids)).sum()
        bad += (~df["account_id"].isin(acct_ids)).sum()
        bad += (~df["month"].astype(str).isin(month_ids)).sum()
    res.append(_result("8_referential_integrity", bad == 0, bad, "all fact keys in dimensions"))

    # 9 salary ties to headcount
    hc = t["fact_headcount"].copy()
    hc["salary_calc"] = (hc["actual_headcount"] * hc["avg_comp_actual"] / 12.0).round(2)
    sal = t["fact_actuals"][t["fact_actuals"]["account_id"].str.endswith("_SAL")].copy()
    sal = sal.merge(hc[["month", "department_id", "salary_calc"]],
                    on=["month", "department_id"], how="left")
    worst = (sal["amount"] - sal["salary_calc"]).abs().max()
    res.append(_result("9_salary_ties_to_headcount", worst <= TOL, worst,
                        "GL salary == HC x comp/12"))

    # 10 rollup ties to lines
    by_month = out["variance_summary"]
    line_sum = line.groupby("month")["var_ab_amount"].sum().round(2)
    merged = by_month.set_index("month")["var_ab_amount"].round(2)
    worst = (merged - line_sum).abs().max()
    res.append(_result("10_rollup_ties_to_lines", worst <= TOL, worst,
                        "company variance == sum of line variances"))

    # 11 operating income identity
    op = out["operating_metrics"]
    diff = (op["operating_income"] - (op["revenue"] - op["cogs"] - op["opex"])).abs().max()
    res.append(_result("11_operating_income_identity", diff <= TOL, diff,
                        "OI == revenue - cogs - opex"))

    return pd.DataFrame(res)


def main():
    df = run_checks()
    os.makedirs(OUT, exist_ok=True)
    df.to_csv(os.path.join(OUT, "validation_results.csv"), index=False)
    width = max(len(c) for c in df["check"])
    print("\nVALIDATION RESULTS")
    print("=" * (width + 30))
    for _, r in df.iterrows():
        print(f"{r['check']:<{width}}  {r['status']:<4}  worst={r['worst_discrepancy']}")
    print("=" * (width + 30))
    n_fail = (df["status"] == "FAIL").sum()
    print(f"{len(df)} checks, {n_fail} failed")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
