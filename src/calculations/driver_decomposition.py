"""
driver_decomposition.py
=======================
Deterministic decomposition of two variances into their drivers.

1) Compensation variance (per department, per month)
   Salary line = headcount x (avg_comp / 12). We split actual-vs-budget salary
   variance into a headcount (volume) effect and a rate effect.

       hc_impact   = (HC_a - HC_b) * Cb_m
       rate_impact = HC_a * (Ca_m - Cb_m)
       hc_impact + rate_impact == (HC_a*Ca_m) - (HC_b*Cb_m) == salary variance

   where Cb_m, Ca_m are MONTHLY comp (annual/12). The interaction term is folded
   into the rate effect by evaluating rate at ACTUAL headcount -- a standard,
   reconciling two-way split. This ties exactly to the GL salary line because
   the salary line was generated as HC x monthly comp (see decision_log.md).

2) Subscription revenue variance (company, per month)
   Monthly subscription revenue = customers x monthly_arpa. We split
   actual-vs-budget revenue variance into a volume (customer) effect and a
   price (ARPA) effect.

       vol_impact   = (Cust_a - Cust_b) * ARPAb_m
       price_impact = Cust_a * (ARPAa_m - ARPAb_m)
       vol_impact + price_impact == revenue variance (favorable when positive)

3) Materiality ranking is provided by variance_engine.add_rankings; here we
   expose a convenience top-N driver selector across the line table.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

RECON_TOL = 0.01  # $ tolerance for reconciliation (rounding only)


def comp_decomposition(headcount: pd.DataFrame) -> pd.DataFrame:
    df = headcount.copy()
    cb_m = df["avg_comp_budget"] / 12.0
    ca_m = df["avg_comp_actual"] / 12.0
    df["salary_budget"] = (df["budget_headcount"] * cb_m).round(2)
    df["salary_actual"] = (df["actual_headcount"] * ca_m).round(2)
    df["salary_variance"] = (df["salary_actual"] - df["salary_budget"]).round(2)
    df["hc_impact"] = ((df["actual_headcount"] - df["budget_headcount"]) * cb_m).round(2)
    df["rate_impact"] = (df["actual_headcount"] * (ca_m - cb_m)).round(2)
    df["decomp_residual"] = (df["salary_variance"]
                             - df["hc_impact"] - df["rate_impact"]).round(2)
    # favorability for an expense: spending LESS than budget is favorable
    df["favorable"] = df["salary_variance"] < 0
    return df[["month", "department_id", "salary_budget", "salary_actual",
               "salary_variance", "hc_impact", "rate_impact", "decomp_residual",
               "favorable"]]


def revenue_decomposition(saas_actual: pd.DataFrame,
                          saas_budget: pd.DataFrame) -> pd.DataFrame:
    a = saas_actual[["month", "customers_end", "ending_arr"]].rename(
        columns={"customers_end": "cust_a", "ending_arr": "arr_a"})
    b = saas_budget[["month", "customers_end", "ending_arr"]].rename(
        columns={"customers_end": "cust_b", "ending_arr": "arr_b"})
    df = a.merge(b, on="month", how="inner")
    # monthly subscription revenue = ARR / 12 ; monthly ARPA = revenue / customers
    df["rev_a"] = (df["arr_a"] / 12.0).round(2)
    df["rev_b"] = (df["arr_b"] / 12.0).round(2)
    df["arpa_a_m"] = (df["rev_a"] / df["cust_a"])
    df["arpa_b_m"] = (df["rev_b"] / df["cust_b"])
    df["rev_variance"] = (df["rev_a"] - df["rev_b"]).round(2)
    df["volume_impact"] = ((df["cust_a"] - df["cust_b"]) * df["arpa_b_m"]).round(2)
    df["price_impact"] = (df["cust_a"] * (df["arpa_a_m"] - df["arpa_b_m"])).round(2)
    df["decomp_residual"] = (df["rev_variance"]
                             - df["volume_impact"] - df["price_impact"]).round(2)
    df["favorable"] = df["rev_variance"] > 0  # more revenue is favorable
    return df[["month", "rev_b", "rev_a", "rev_variance",
               "volume_impact", "price_impact", "decomp_residual", "favorable"]]


def top_drivers(line_df: pd.DataFrame, month: str, n: int = 5,
                department_id: str | None = None) -> pd.DataFrame:
    """Top-N materiality-ranked drivers for a month (optionally one department)."""
    df = line_df[line_df["month"] == month].copy()
    if department_id:
        df = df[df["department_id"] == department_id]
    df["abs_oi_impact"] = df["oi_impact_ab"].abs()
    cols = ["month", "department_id", "account_name", "actual", "budget",
            "var_ab_amount", "oi_impact_ab", "favorable_ab", "var_ab_pct"]
    return (df.sort_values("abs_oi_impact", ascending=False)
              .head(n)[cols].reset_index(drop=True))
