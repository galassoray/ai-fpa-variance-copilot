"""
saas_metrics.py
===============
Deterministic SaaS KPI layer. Consumes the ARR/customer bridge plus the P&L
fact tables and headcount, and emits a monthly KPI table.

All figures are computed here in code; nothing is estimated or model-generated.

Metrics
-------
ARR bridge check : starting + new + expansion - contraction - churned == ending
NRR (monthly)    : (starting + expansion - contraction - churned) / starting
GRR (monthly)    : (starting - contraction - churned) / starting
NRR_ttm / GRR_ttm: trailing-12-month versions (NULL until 12 months exist)
logo_churn_rate  : churned_customers / customers_start
arpa             : ending_arr / customers_end (annual)
arr_per_head     : ending_arr / total_headcount
revenue_per_head : (monthly subscription+services revenue * 12) / total_headcount
gross_margin     : (revenue - cogs) / revenue
opex_pct_revenue : opex / revenue
dept_opex_pct_revenue : department opex / revenue
operating_income : revenue - cogs - opex   (EBITDA == OI here; no D&A modeled)
headcount vs plan: actual - budget by department
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def arr_bridge_check(saas: pd.DataFrame) -> pd.DataFrame:
    df = saas.copy()
    df["ending_arr_calc"] = (df["starting_arr"] + df["new_arr"] + df["expansion_arr"]
                             - df["contraction_arr"] - df["churned_arr"]).round(2)
    df["bridge_diff"] = (df["ending_arr_calc"] - df["ending_arr"]).round(2)
    return df


def ttm_retention(saas: pd.DataFrame) -> pd.DataFrame:
    """Trailing-12-month NRR/GRR off the starting ARR 12 months prior."""
    df = saas.sort_values("month").reset_index(drop=True).copy()
    nrr_ttm, grr_ttm = [], []
    for i in range(len(df)):
        if i < 12:
            nrr_ttm.append(np.nan); grr_ttm.append(np.nan); continue
        base = df.loc[i - 12, "starting_arr"]
        window = df.loc[i - 11:i]
        exp = window["expansion_arr"].sum()
        con = window["contraction_arr"].sum()
        chu = window["churned_arr"].sum()
        nrr_ttm.append(round((base + exp - con - chu) / base, 5))
        grr_ttm.append(round((base - con - chu) / base, 5))
    df["nrr_ttm"] = nrr_ttm
    df["grr_ttm"] = grr_ttm
    return df


def operating_metrics(actuals: pd.DataFrame, account_meta: pd.DataFrame,
                      headcount: pd.DataFrame, saas: pd.DataFrame) -> pd.DataFrame:
    """Monthly company operating metrics from the actuals P&L."""
    a = actuals.merge(account_meta[["account_id", "is_revenue", "is_cogs", "is_opex",
                                     "department_id"]],
                      on=["account_id", "department_id"], how="left")

    def msum(mask):
        return (a[mask].groupby("month")["amount"].sum())

    rev = msum(a["is_revenue"] == 1).rename("revenue")
    cogs = msum(a["is_cogs"] == 1).rename("cogs")
    opex = msum(a["is_opex"] == 1).rename("opex")
    m = pd.concat([rev, cogs, opex], axis=1).fillna(0.0).reset_index()

    m["gross_profit"] = (m["revenue"] - m["cogs"]).round(2)
    m["gross_margin"] = (m["gross_profit"] / m["revenue"]).round(5)
    m["operating_income"] = (m["revenue"] - m["cogs"] - m["opex"]).round(2)
    m["operating_margin"] = (m["operating_income"] / m["revenue"]).round(5)
    m["opex_pct_revenue"] = (m["opex"] / m["revenue"]).round(5)

    hc_tot = headcount.groupby("month")["actual_headcount"].sum().rename("total_headcount")
    m = m.merge(hc_tot.reset_index(), on="month", how="left")

    arr = saas[["month", "ending_arr"]]
    m = m.merge(arr, on="month", how="left")
    m["arr_per_head"] = (m["ending_arr"] / m["total_headcount"]).round(2)
    m["revenue_per_head"] = (m["revenue"] * 12 / m["total_headcount"]).round(2)
    return m.round(2)


def dept_opex_pct_revenue(actuals: pd.DataFrame, account_meta: pd.DataFrame) -> pd.DataFrame:
    a = actuals.merge(account_meta[["account_id", "is_revenue", "is_opex", "department_id"]],
                      on=["account_id", "department_id"], how="left")
    rev = a[a["is_revenue"] == 1].groupby("month")["amount"].sum().rename("revenue")
    dept = (a[a["is_opex"] == 1].groupby(["month", "department_id"])["amount"].sum()
            .rename("dept_opex").reset_index())
    dept = dept.merge(rev.reset_index(), on="month", how="left")
    dept["dept_opex_pct_revenue"] = (dept["dept_opex"] / dept["revenue"]).round(5)
    return dept


def headcount_vs_plan(headcount: pd.DataFrame) -> pd.DataFrame:
    df = headcount.copy()
    df["hc_var_vs_budget"] = df["actual_headcount"] - df["budget_headcount"]
    df["hc_var_vs_forecast"] = df["actual_headcount"] - df["forecast_headcount"]
    return df[["month", "department_id", "actual_headcount", "budget_headcount",
               "forecast_headcount", "hc_var_vs_budget", "hc_var_vs_forecast"]]
