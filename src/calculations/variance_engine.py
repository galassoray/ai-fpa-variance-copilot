"""
variance_engine.py
==================
Deterministic variance computation. NO LLM, NO randomness. Every number a
decision-maker could see is produced here, in code.

Core definitions
----------------
variance_amount      = actual - budget                 (raw, magnitude space)
oi_impact            = oi_sign * variance_amount        (signed effect on
                       operating income; + = favorable, - = unfavorable)
favorable (bool)     = oi_impact > 0
pct_variance         = variance_amount / budget, set to NULL ("n/m") when the
                       budget base is below MATERIALITY_FLOOR (guards div-by-~0)
contribution_to_oi   = oi_impact / total_oi_impact      (signed share of the
                       net operating-income variance at the chosen rollup)
materiality_rank     = rank by ABSOLUTE oi_impact (1 = largest mover)

The favorable/unfavorable flag is ACCOUNT-AWARE: it is driven entirely by
dim_account.oi_sign, never by the bare sign of (actual - budget).
"""
from __future__ import annotations
import numpy as np
import pandas as pd

MATERIALITY_FLOOR = 100.0  # $ ; below this a % variance is "not meaningful"


def _signed(account_meta: pd.DataFrame, facts: pd.DataFrame) -> pd.DataFrame:
    return facts.merge(
        account_meta[["account_id", "account_name", "department_id",
                      "account_category", "statement_line", "favorable_direction",
                      "is_revenue", "is_cogs", "is_opex", "is_expense", "oi_sign"]],
        on=["account_id", "department_id"], how="left",
    )


def line_variance(actuals: pd.DataFrame, budget: pd.DataFrame,
                  account_meta: pd.DataFrame,
                  forecast: pd.DataFrame | None = None) -> pd.DataFrame:
    """Account x department x month variance table (the lowest grain)."""
    a = actuals.rename(columns={"amount": "actual"})
    b = budget.rename(columns={"amount": "budget"})
    df = a.merge(b, on=["month", "department_id", "account_id"], how="outer")
    if forecast is not None:
        f = forecast.rename(columns={"amount": "forecast"})
        df = df.merge(f, on=["month", "department_id", "account_id"], how="left")
    else:
        df["forecast"] = np.nan

    df[["actual", "budget"]] = df[["actual", "budget"]].fillna(0.0)
    df = _signed(account_meta, df)

    # actual vs budget
    df["var_ab_amount"] = (df["actual"] - df["budget"]).round(2)
    df["oi_impact_ab"] = (df["oi_sign"] * df["var_ab_amount"]).round(2)
    df["favorable_ab"] = df["oi_impact_ab"] > 0
    df["var_ab_pct"] = _safe_pct(df["var_ab_amount"], df["budget"])
    df["ab_pct_meaningful"] = df["budget"].abs() >= MATERIALITY_FLOOR

    # actual vs forecast (only where forecast present)
    has_fc = df["forecast"].notna()
    df["var_af_amount"] = np.where(has_fc, (df["actual"] - df["forecast"]).round(2), np.nan)
    df["oi_impact_af"] = np.where(has_fc, (df["oi_sign"] * df["var_af_amount"]).round(2), np.nan)
    df["favorable_af"] = np.where(has_fc, df["oi_impact_af"] > 0, np.nan)
    df["var_af_pct"] = np.where(has_fc, _safe_pct(df["actual"] - df["forecast"], df["forecast"]), np.nan)

    return df.sort_values(["month", "department_id", "account_id"]).reset_index(drop=True)


def _safe_pct(num: pd.Series, base: pd.Series) -> pd.Series:
    base = pd.Series(base).astype(float)
    num = pd.Series(num).astype(float)
    out = np.where(base.abs() >= MATERIALITY_FLOOR, num / base, np.nan)
    return np.round(out, 5)


def add_rankings(line_df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    """Contribution-to-OI-variance and absolute materiality rank within a group."""
    df = line_df.copy()
    grp = df.groupby(group_cols)["oi_impact_ab"]
    total = grp.transform("sum")
    df["group_total_oi_impact_ab"] = total.round(2)
    df["contribution_to_oi_ab"] = np.where(
        total.abs() >= MATERIALITY_FLOOR, (df["oi_impact_ab"] / total).round(5), np.nan)
    df["materiality_rank"] = (
        df.assign(absimp=df["oi_impact_ab"].abs())
          .groupby(group_cols)["absimp"]
          .rank(ascending=False, method="first").astype(int)
    )
    return df


def add_trend(line_df: pd.DataFrame) -> pd.DataFrame:
    """Month-over-month actual movement at the line grain."""
    df = line_df.sort_values(["department_id", "account_id", "month"]).copy()
    g = df.groupby(["department_id", "account_id"])["actual"]
    df["actual_mom_amount"] = g.diff().round(2)
    df["actual_mom_pct"] = (df["actual_mom_amount"] / g.shift(1)).round(5)
    return df


def add_ytd(line_df: pd.DataFrame, dim_date: pd.DataFrame) -> pd.DataFrame:
    """Year-to-date actual/budget and YTD variance within fiscal year."""
    fy = dim_date[["month", "fiscal_year", "month_number"]]
    df = line_df.merge(fy, on="month", how="left").sort_values(
        ["department_id", "account_id", "fiscal_year", "month_number"])
    keys = ["department_id", "account_id", "fiscal_year"]
    df["actual_ytd"] = df.groupby(keys)["actual"].cumsum().round(2)
    df["budget_ytd"] = df.groupby(keys)["budget"].cumsum().round(2)
    df["var_ab_ytd_amount"] = (df["actual_ytd"] - df["budget_ytd"]).round(2)
    df["oi_impact_ytd"] = (df["oi_sign"] * df["var_ab_ytd_amount"]).round(2)
    return df


def rollup(line_df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Aggregate variance to any grain (e.g., department, statement_line, month)."""
    agg = (line_df.groupby(by)
           .agg(actual=("actual", "sum"),
                budget=("budget", "sum"),
                forecast=("forecast", "sum"),
                var_ab_amount=("var_ab_amount", "sum"),
                oi_impact_ab=("oi_impact_ab", "sum"))
           .reset_index())
    agg["var_ab_pct"] = _safe_pct(agg["var_ab_amount"], agg["budget"])
    agg["favorable_ab"] = agg["oi_impact_ab"] > 0
    return agg.round(2)
