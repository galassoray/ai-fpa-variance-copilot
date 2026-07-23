"""
projection.py
=============
Deterministic forward projection for the forecast view. NO LLM. NO randomness.

The method is an explicit, visible choice rather than a hidden assumption --
because "how did you forecast this?" is the first question an interviewer asks,
and a single unlabeled trend line is not an answer. Each method states what it
assumes and what it ignores:

  linear_trend : ordinary least squares on the trailing `lookback` months,
                 extrapolated forward.
                 Assumes: the recent trend continues in a straight line.
                 Ignores: seasonality, step changes, saturation.
                 Reports an R^2 so a weak fit is visible rather than implied.

  run_rate     : mean of the trailing `lookback_short` months, held flat.
                 Assumes: the business continues at its current level.
                 Ignores: growth entirely. The conservative baseline; useful
                 precisely because it makes no growth claim.

  driver_arr   : (revenue only) continues the ARR bridge forward using the
                 trailing average of new / expansion / contraction / churned
                 ARR, then derives revenue as ending ARR / 12.
                 Assumes: the bridge components persist at their recent average.
                 Ignores: pipeline, seasonality, cohort effects.
                 This is the driver-based method -- it only exists where real
                 drivers exist, which is the honest limit of the approach.

Every projected number is computed here in code. The narrative layer may state
the method and its assumption; it never picks the number.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

METHODS = {
    "linear_trend": "Linear trend (OLS on trailing months)",
    "run_rate": "Run-rate (recent average, held flat)",
    "driver_arr": "Driver-based (ARR bridge continued)",
}


@dataclass
class Projection:
    months: list = field(default_factory=list)     # future month strings
    values: list = field(default_factory=list)     # projected values
    method: str = ""
    assumption: str = ""
    r2: float | None = None
    fitted_on: int = 0


def future_months(last_month: str, horizon: int) -> list:
    """Month-start strings following `last_month`."""
    start = pd.Timestamp(last_month) + pd.DateOffset(months=1)
    idx = pd.date_range(start, periods=horizon, freq="MS")
    return [d.date().isoformat() for d in idx]


def _ols(y: np.ndarray):
    """Fit y = a*x + b over x = 0..n-1. Returns (a, b, r2)."""
    n = len(y)
    x = np.arange(n, dtype=float)
    if n < 2:
        return 0.0, float(y[-1]) if n else 0.0, None
    a, b = np.polyfit(x, y, 1)
    pred = a * x + b
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    return float(a), float(b), r2


def project_series(df: pd.DataFrame, value_col: str, method: str = "linear_trend",
                   horizon: int = 6, lookback: int = 12,
                   month_col: str = "month") -> Projection:
    """Project one monthly series forward. `df` must be sorted ascending."""
    d = df[[month_col, value_col]].dropna().sort_values(month_col)
    if d.empty:
        return Projection(method=method, assumption="no data")
    y_all = d[value_col].to_numpy(dtype=float)
    last = str(d[month_col].iloc[-1])
    fmonths = future_months(last, horizon)

    if method == "run_rate":
        k = min(3, len(y_all))
        level = float(np.mean(y_all[-k:]))
        return Projection(fmonths, [round(level, 2)] * horizon, "run_rate",
                          f"held flat at the trailing {k}-month average", None, k)

    # default: linear trend
    k = min(lookback, len(y_all))
    y = y_all[-k:]
    a, b, r2 = _ols(y)
    vals = [round(float(a * (k - 1 + h) + b), 2) for h in range(1, horizon + 1)]
    return Projection(fmonths, vals, "linear_trend",
                      f"trailing {k}-month linear trend continues", r2, k)


def project_revenue_driver(saas: pd.DataFrame, horizon: int = 6,
                           lookback: int = 3) -> Projection:
    """Continue the ARR bridge forward, then derive monthly revenue = ARR / 12."""
    d = saas.sort_values("month")
    if d.empty:
        return Projection(method="driver_arr", assumption="no data")
    k = min(lookback, len(d))
    tail = d.tail(k)
    new = float(tail["new_arr"].mean())
    exp = float(tail["expansion_arr"].mean())
    con = float(tail["contraction_arr"].mean())
    chu = float(tail["churned_arr"].mean())

    arr = float(d["ending_arr"].iloc[-1])
    fmonths = future_months(str(d["month"].iloc[-1]), horizon)
    vals = []
    for _ in range(horizon):
        arr = arr + new + exp - con - chu
        vals.append(round(arr / 12.0, 2))
    return Projection(fmonths, vals, "driver_arr",
                      f"ARR bridge continued at the trailing {k}-month average "
                      f"of new/expansion/contraction/churn", None, k)


def build_projection_frame(hist: pd.DataFrame, actual_col: str, budget_col: str,
                           method: str, horizon: int, lookback: int = 12,
                           saas_actual: pd.DataFrame | None = None,
                           saas_budget: pd.DataFrame | None = None) -> tuple:
    """
    Return (long_df, meta) where long_df has columns
    month | series | value | kind  with kind in {'history','projected'} and
    series in {'Actual','Budget'} -- ready to chart.
    """
    rows = []
    for _, r in hist.iterrows():
        rows.append({"month": r["month"], "series": "Actual",
                     "value": float(r[actual_col]), "kind": "history"})
        rows.append({"month": r["month"], "series": "Budget",
                     "value": float(r[budget_col]), "kind": "history"})

    if method == "driver_arr" and saas_actual is not None:
        pa = project_revenue_driver(saas_actual, horizon)
        pb = (project_revenue_driver(saas_budget, horizon)
              if saas_budget is not None else
              project_series(hist, budget_col, "linear_trend", horizon, lookback))
    else:
        pa = project_series(hist, actual_col, method, horizon, lookback)
        pb = project_series(hist, budget_col, method, horizon, lookback)

    # bridge the dashed line back to the last actual point so it connects
    last = hist.iloc[-1]
    rows.append({"month": last["month"], "series": "Actual",
                 "value": float(last[actual_col]), "kind": "projected"})
    rows.append({"month": last["month"], "series": "Budget",
                 "value": float(last[budget_col]), "kind": "projected"})
    for m, v in zip(pa.months, pa.values):
        rows.append({"month": m, "series": "Actual", "value": v, "kind": "projected"})
    for m, v in zip(pb.months, pb.values):
        rows.append({"month": m, "series": "Budget", "value": v, "kind": "projected"})

    meta = {"method": METHODS.get(method, method),
            "assumption": pa.assumption, "r2": pa.r2,
            "fitted_on": pa.fitted_on,
            "actual": pa, "budget": pb}
    return pd.DataFrame(rows), meta
