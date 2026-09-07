"""
make_january.py
===============
Writes a realistic set of CSVs for the next unloaded month, so the upload
feature can be demonstrated without hand-building files.

    python make_january.py

Produces `upload-sample/` containing actuals, budget, headcount, SaaS metrics
and the SaaS metrics budget for the month after the dataset ends. The figures
are the prior month grown by a modest amount with a little noise -- enough that
the variances are real rather than zero, and enough that the loaded month does
not look like a copy.

SYNTHETIC ONLY. This exists so a demonstration never needs a real file. Nothing
derived from an employer should ever be uploaded to this tool.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

import run_pipeline as rp  # noqa: E402

OUT = os.path.join(HERE, "upload-sample")

#: Grown from the prior month so the new period has genuine variances. Actuals
#: and budget drift apart deliberately: a month where everything lands on plan
#: demonstrates nothing.
ACTUAL_GROWTH = 1.021
BUDGET_GROWTH = 1.028
NOISE = 0.035


def next_month(latest: str) -> str:
    year, month = int(latest[:4]), int(latest[5:7])
    return f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"


def main() -> int:
    tables = rp.load()
    latest = max(tables["fact_actuals"]["month"].astype(str))
    target = next_month(latest[:7])
    rng = np.random.default_rng(20260101)

    os.makedirs(OUT, exist_ok=True)
    written = []

    def prior(name: str) -> pd.DataFrame:
        df = tables[name]
        out = df[df["month"].astype(str).str.startswith(latest[:7])].copy()
        out["month"] = target
        return out.reset_index(drop=True)

    for name, fname, growth in (
        ("fact_actuals", f"{target}_actuals.csv", ACTUAL_GROWTH),
        ("fact_budget", f"{target}_budget.csv", BUDGET_GROWTH),
    ):
        df = prior(name)
        jitter = 1 + rng.normal(0, NOISE, len(df))
        df["amount"] = (df["amount"].astype(float) * growth * jitter).round(2)
        df.to_csv(os.path.join(OUT, fname), index=False)
        written.append(fname)

    hc = prior("fact_headcount")
    for col in ("actual_headcount", "budget_headcount", "forecast_headcount"):
        if col in hc.columns:
            hc[col] = hc[col].astype(int)
    hc.loc[hc.index[:1], "actual_headcount"] = \
        hc.loc[hc.index[:1], "actual_headcount"].astype(int) + 2
    for col in ("avg_comp_actual", "avg_comp_budget", "avg_comp_forecast"):
        if col in hc.columns:
            hc[col] = (hc[col].astype(float) * 1.004).round(2)
    hc.to_csv(os.path.join(OUT, f"{target}_headcount.csv"), index=False)
    written.append(f"{target}_headcount.csv")

    for name, fname in (
        ("fact_saas_metrics", f"{target}_saas_metrics.csv"),
        ("fact_saas_metrics_budget", f"{target}_saas_metrics_budget.csv"),
    ):
        if name not in tables:
            continue
        df = prior(name)
        # Ending ARR of the prior month becomes the starting ARR of this one,
        # so the bridge ties instead of restating a stale opening balance.
        prev = tables[name]
        prev = prev[prev["month"].astype(str).str.startswith(latest[:7])]
        if "ending_arr" in prev.columns and not prev.empty:
            df["starting_arr"] = float(prev["ending_arr"].iloc[0])
        for col in ("new_arr", "expansion_arr", "contraction_arr",
                    "churned_arr"):
            if col in df.columns:
                df[col] = (df[col].astype(float)
                           * (1 + rng.normal(0, 0.05))).round(2)
        if {"starting_arr", "new_arr", "expansion_arr", "contraction_arr",
                "churned_arr", "ending_arr"} <= set(df.columns):
            df["ending_arr"] = (df["starting_arr"] + df["new_arr"]
                                + df["expansion_arr"] - df["contraction_arr"]
                                - df["churned_arr"]).round(2)
        df.to_csv(os.path.join(OUT, fname), index=False)
        written.append(fname)

    print(f"\nWrote {len(written)} file(s) for {target} to {OUT}\n")
    for f in written:
        print(f"  {f}")
    print("\nUpload them in the app: sidebar -> Load a new period from CSV")
    print("Synthetic data. Never upload anything derived from an employer.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
