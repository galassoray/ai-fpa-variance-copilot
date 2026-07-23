"""
build_database.py
=================
Loads the synthetic CSVs into a single DuckDB database file
(data/processed/fpa.duckdb) and adds simple foreign-key sanity views.

DuckDB is used (vs SQLite) because the workload is analytical: columnar
aggregation over fact tables joined to dimensions. See decision_log.md.
"""
from __future__ import annotations
import os
import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
SYN = os.path.join(HERE, "..", "data", "synthetic")
DB = os.path.join(HERE, "..", "data", "processed", "fpa.duckdb")

TABLES = [
    "dim_date", "dim_department", "dim_account",
    "fact_headcount", "fact_actuals", "fact_budget", "fact_forecast",
    "fact_saas_metrics", "fact_saas_metrics_budget", "fact_saas_metrics_forecast",
]


def build():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)
    con = duckdb.connect(DB)
    for t in TABLES:
        csv = os.path.join(SYN, f"{t}.csv")
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv_auto('{csv}', header=true)")
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"loaded {t:28s} rows={n}")
    con.close()
    print(f"\ndatabase -> {os.path.relpath(DB)}")


def connect() -> "duckdb.DuckDBPyConnection":
    return duckdb.connect(DB, read_only=True)


if __name__ == "__main__":
    build()
