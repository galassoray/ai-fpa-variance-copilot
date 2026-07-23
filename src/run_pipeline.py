"""
run_pipeline.py
===============
End-to-end deterministic pipeline: read synthetic data from DuckDB, compute the
full variance + SaaS + driver layer, write output tables to outputs/.

No LLM. No randomness. Reproducible.
"""
from __future__ import annotations
import os
import duckdb
import pandas as pd

from calculations import variance_engine as ve
from calculations import saas_metrics as sm
from calculations import driver_decomposition as dd

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "data", "processed", "fpa.duckdb")
OUT = os.path.join(HERE, "..", "outputs")
os.makedirs(OUT, exist_ok=True)


def ensure_database():
    """Materialize the canonical DuckDB dataset if it hasn't been built yet.

    The .duckdb file is a build artifact (gitignored, not packaged), so a fresh
    clone or extraction won't have one. Rather than failing with an IO error,
    build it from the packaged synthetic CSVs -- regenerating them from the
    seeded generator first if they're absent too. The result is identical either
    way, because the generator is seeded.
    """
    if os.path.exists(DB):
        return
    syn = os.path.join(HERE, "..", "data", "synthetic", "fact_actuals.csv")
    if not os.path.exists(syn):
        import generate_synthetic_data as gen
        print("[setup] synthetic data not found — generating from the seeded generator")
        gen.main()
    import build_database as bdb
    print("[setup] database not found — building it from the synthetic CSVs")
    bdb.build()


def load():
    ensure_database()
    con = duckdb.connect(DB, read_only=True)
    t = {n: con.execute(f"SELECT * FROM {n}").df() for n in [
        "dim_date", "dim_department", "dim_account",
        "fact_actuals", "fact_budget", "fact_forecast", "fact_headcount",
        "fact_saas_metrics", "fact_saas_metrics_budget", "fact_saas_metrics_forecast",
    ]}
    con.close()
    for name, df in t.items():
        if "month" in df.columns:
            df["month"] = df["month"].astype(str)
    return t


def compute(t: dict) -> dict:
    """Compute every output table from a set of source tables.

    Separated from load() so a live scenario can be computed in memory from the
    same table structures, without round-tripping through the database file.
    The canonical dataset still lives in DuckDB; this is the identical schema
    and the identical computation -- there is no second, shadow implementation.
    """
    t = {k: (v.assign(month=v["month"].astype(str)) if "month" in v.columns else v)
         for k, v in t.items()}
    acct = t["dim_account"]

    # ---- variance line table (lowest grain) ----
    line = ve.line_variance(t["fact_actuals"], t["fact_budget"], acct, t["fact_forecast"])
    line = ve.add_trend(line)
    line = ve.add_ytd(line, t["dim_date"])
    line = ve.add_rankings(line, group_cols=["month"])          # company-wide rank
    line = ve.add_rankings(line.rename(columns={"materiality_rank": "materiality_rank_co",
                                                "contribution_to_oi_ab": "contribution_to_oi_co"}),
                           group_cols=["month", "department_id"])  # within-dept rank

    # ---- rollups ----
    by_dept = ve.rollup(line, ["month", "department_id"])
    by_stmt = ve.rollup(line, ["month", "statement_line"])
    by_month = ve.rollup(line, ["month"])

    # ---- SaaS metrics ----
    saas_chk = sm.arr_bridge_check(t["fact_saas_metrics"])
    saas_ttm = sm.ttm_retention(saas_chk)
    op = sm.operating_metrics(t["fact_actuals"], acct, t["fact_headcount"], t["fact_saas_metrics"])
    dept_opex = sm.dept_opex_pct_revenue(t["fact_actuals"], acct)
    hc_plan = sm.headcount_vs_plan(t["fact_headcount"])

    # ---- driver decompositions ----
    comp = dd.comp_decomposition(t["fact_headcount"])
    rev = dd.revenue_decomposition(t["fact_saas_metrics"], t["fact_saas_metrics_budget"])

    outputs = {
        "variance_detail": line,
        "variance_by_department": by_dept,
        "variance_by_statement_line": by_stmt,
        "variance_summary": by_month,
        "saas_metrics_summary": saas_ttm,
        "operating_metrics": op,
        "dept_opex_pct_revenue": dept_opex,
        "headcount_vs_plan": hc_plan,
        "comp_decomposition": comp,
        "revenue_decomposition": rev,
    }
    return outputs


def run(write=True):
    """Load the canonical dataset from DuckDB and compute everything."""
    outputs = compute(load())
    if write:
        for name, df in outputs.items():
            df.to_csv(os.path.join(OUT, f"{name}.csv"), index=False)
            print(f"wrote {name:28s} rows={len(df)}")
    return outputs


if __name__ == "__main__":
    run()
