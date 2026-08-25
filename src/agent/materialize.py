"""
materialize.py
==============
Publishes the deterministic computation layer's outputs into DuckDB as queryable
marts (``out_*`` tables), so the agent's tools can be parameterized SQL.

WHY THIS EXISTS
---------------
``run_pipeline`` computes everything in pandas: it reads the star schema out of
DuckDB with ``SELECT *`` and does every variance, ranking, and decomposition in
memory. DuckDB is a storage layer in the copilot, not a computation layer.

The agent needs a SQL tool surface. There were three ways to get one:

  1. Wrap the pandas ``outputs`` dict in tool functions. Zero divergence risk,
     but no SQL exists anywhere.
  2. Write new SQL against ``fact_actuals`` / ``fact_budget`` / ``dim_account``
     that recomputes variance. This creates a *second implementation* of the
     variance math -- two sign conventions, two materiality floors, two
     pct-safety rules -- which is exactly what run_pipeline's docstring says the
     codebase does not have.
  3. Materialize the already-computed outputs as marts and write SQL over those.

Option 3 is what a production stack actually looks like: a transformation tier
computes marts on a schedule, and analysts query the marts. Nobody recomputes
revenue recognition in an ad-hoc query. Here the pandas layer *is* the
transformation tier and ``out_variance_detail`` is a mart.

The consequence for the deterministic/LLM boundary is the point of the whole
design: SQL may **slice, filter, join, rank, and aggregate** figures the
canonical layer already computed. It may not recompute them. One implementation
of the math, one source of truth.

STALENESS
---------
The cost of materialization is that a mart can go stale: if the pandas layer
changes and this step is not re-run, the agent serves old numbers with a clean
audit trail attesting to them. That is the single worst failure mode this design
introduces, so it is closed structurally rather than by discipline.

``build_hash`` fingerprints both the *inputs* (synthetic CSVs) and the
*computation* (the source of every module that produces an output table). Any
change to either produces a different hash. ``assert_fresh()`` refuses to serve
against a stale mart, and the agent calls it at startup. A refusal here is the
correct outcome, not an inconvenience.
"""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime, timezone

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, ".."))

# Make the sibling modules (run_pipeline, calculations, narrative) importable
# whether this file is run as a script, run as -m, or imported from a test that
# has already put src on the path.
if SRC not in sys.path:
    sys.path.insert(0, SRC)
DB = os.path.join(SRC, "..", "data", "processed", "fpa.duckdb")
SYN = os.path.join(SRC, "..", "data", "synthetic")

META_TABLE = "_agent_build_meta"

#: Source files whose content defines the computation. A change to any of these
#: invalidates the marts. Kept explicit rather than globbing ``src/`` so that
#: editing an unrelated module (the app, the narrative layer) does not
#: needlessly invalidate a mart it cannot affect.
COMPUTATION_SOURCES = [
    "run_pipeline.py",
    os.path.join("calculations", "variance_engine.py"),
    os.path.join("calculations", "saas_metrics.py"),
    os.path.join("calculations", "driver_decomposition.py"),
    os.path.join("agent", "materialize.py"),
]

#: outputs dict key -> mart table name. Explicit rather than derived so that
#: renaming an output key is a visible, reviewable change to the tool surface.
MART_TABLES = {
    "variance_detail": "out_variance_detail",
    "variance_by_department": "out_variance_by_department",
    "variance_by_statement_line": "out_variance_by_statement_line",
    "variance_summary": "out_variance_summary",
    "saas_metrics_summary": "out_saas_metrics",
    "operating_metrics": "out_operating_metrics",
    "dept_opex_pct_revenue": "out_dept_opex_pct_revenue",
    "headcount_vs_plan": "out_headcount_vs_plan",
    "comp_decomposition": "out_comp_decomposition",
    "revenue_decomposition": "out_revenue_decomposition",
}


# --------------------------------------------------------------------------
# build hash
# --------------------------------------------------------------------------
def _hash_file(h: "hashlib._Hash", path: str) -> None:
    """Mix one file's *content* into the digest, normalized for line endings.

    Hashing raw bytes would make the fingerprint depend on how git happened to
    check the file out. A Windows clone with ``core.autocrlf`` gets CRLF, a
    Linux clone and Streamlit Community Cloud get LF, and the identical logical
    dataset would fingerprint differently on each -- so the hash would be
    useless as a cross-environment integrity signal, and comparing it between a
    laptop and a deployment would produce a spurious mismatch that looks exactly
    like real staleness.

    CRLF and lone CR are normalized to LF before hashing. Nothing else is
    normalized: whitespace and content changes must still change the digest,
    because catching a changed computation layer is the entire point.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    h.update(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))


def build_hash() -> str:
    """Fingerprint of (synthetic inputs + computation source).

    Deterministic and order-independent: files are hashed in sorted order with
    their basename mixed in, so a rename is a change.
    """
    h = hashlib.sha256()

    if os.path.isdir(SYN):
        for name in sorted(os.listdir(SYN)):
            if name.endswith(".csv"):
                h.update(name.encode())
                _hash_file(h, os.path.join(SYN, name))

    for rel in sorted(COMPUTATION_SOURCES):
        path = os.path.join(SRC, rel)
        if os.path.exists(path):
            h.update(rel.replace("\\", "/").encode())
            _hash_file(h, path)

    return h.hexdigest()[:16]


SOURCE_META = "_agent_source_meta"


def csv_fingerprint() -> str:
    """Fingerprint of the packaged synthetic CSVs alone.

    Separate from ``build_hash`` because it answers a different question: not
    "are the marts current?" but "do the DuckDB base tables still match the
    source data they were built from?"
    """
    h = hashlib.sha256()
    if os.path.isdir(SYN):
        for name in sorted(os.listdir(SYN)):
            if name.endswith(".csv"):
                h.update(name.encode())
                _hash_file(h, os.path.join(SYN, name))
    return h.hexdigest()[:16]


def ensure_base_tables_current(verbose: bool = True) -> bool:
    """Rebuild the DuckDB base tables if the source CSVs have changed.

    WHY THIS IS NECESSARY -- and it is the subtlest failure in the whole design.

    ``run_pipeline.ensure_database()`` builds the DuckDB file from the CSVs only
    when the file is *absent*. Once it exists, it is never rebuilt, so the
    database is a cache that is never invalidated. ``load()`` reads that cache.

    ``build_hash`` fingerprints the CSVs. Put those two facts together and the
    original implementation had a false-green: edit a CSV, re-run
    materialization, and the marts get computed from *stale base tables* but
    stamped with a hash derived from the *new CSVs*. The freshness check would
    report current while certifying data it had never read.

    A false green is strictly worse than the stale-mart problem the hash exists
    to prevent -- a check that lies is worse than no check, because it is
    trusted. So the chain is closed here: the CSVs are the committed source of
    truth, the ``.duckdb`` file is a gitignored build artifact, and this
    function invalidates the artifact when its source moves.

    Returns True if a rebuild happened.
    """
    want = csv_fingerprint()
    have = None
    if os.path.exists(DB):
        try:
            con = duckdb.connect(DB, read_only=True)
            try:
                row = con.execute(f"SELECT csv_fingerprint FROM {SOURCE_META} LIMIT 1").fetchone()
                have = row[0] if row else None
            finally:
                con.close()
        except duckdb.Error:
            have = None

    if have == want:
        return False

    import build_database as bdb
    import run_pipeline as rp

    rp.ensure_database()          # generates CSVs then builds, if nothing exists yet
    if os.path.exists(DB) and have != want:
        if verbose:
            reason = "no source fingerprint recorded" if have is None else \
                     f"source changed ({have} -> {want})"
            print(f"[materialize] rebuilding base tables from CSVs: {reason}")
        bdb.build()               # deletes and recreates the file from the CSVs

    con = duckdb.connect(DB)
    try:
        con.execute(f"CREATE OR REPLACE TABLE {SOURCE_META} "
                    "(csv_fingerprint VARCHAR, recorded_at_utc TIMESTAMP)")
        con.execute(f"INSERT INTO {SOURCE_META} VALUES (?, ?)",
                    [want, datetime.now(timezone.utc).replace(tzinfo=None)])
    finally:
        con.close()
    return True


def stored_hash(con: "duckdb.DuckDBPyConnection") -> str | None:
    """Read the hash recorded at the last materialization, or None."""
    try:
        row = con.execute(
            f"SELECT build_hash FROM {META_TABLE} ORDER BY built_at_utc DESC LIMIT 1"
        ).fetchone()
    except duckdb.Error:
        return None
    return row[0] if row else None


class StaleMartError(RuntimeError):
    """Raised when the marts do not match the current computation layer."""


def assert_fresh(con: "duckdb.DuckDBPyConnection") -> None:
    """Refuse to serve the agent against stale or missing marts.

    Called at agent startup. Refusing is the correct behavior: serving a stale
    mart would produce numbers that pass every downstream audit while being
    wrong, which is the one failure this architecture must not permit.
    """
    have = stored_hash(con)
    want = build_hash()
    if have is None:
        raise StaleMartError(
            "agent marts have not been built. Run: python src/agent/materialize.py"
        )
    if have != want:
        raise StaleMartError(
            f"agent marts are stale (mart={have}, current={want}). The synthetic data "
            f"or the computation layer changed after the last materialization. "
            f"Re-run: python src/agent/materialize.py"
        )


# --------------------------------------------------------------------------
# materialization
# --------------------------------------------------------------------------
def materialize(outputs: dict | None = None, verbose: bool = True) -> str:
    """Write the computed outputs into DuckDB as ``out_*`` marts.

    Idempotent: tables are replaced wholesale, so re-running is safe and
    produces byte-identical marts for identical inputs (the generator is
    seeded and the computation is deterministic).

    Returns the build hash that was recorded.
    """
    import run_pipeline as rp  # local import: keeps agent import-light

    # Close the source->cache->mart chain before reading anything. Without this,
    # marts can be computed from stale base tables and stamped with a hash
    # derived from CSVs that were never actually read. See
    # ensure_base_tables_current for why that false-green matters.
    rebuilt = ensure_base_tables_current(verbose=verbose)
    if outputs is None or rebuilt:
        outputs = rp.compute(rp.load())

    missing = set(MART_TABLES) - set(outputs)
    if missing:
        raise KeyError(f"computation layer did not produce expected outputs: {sorted(missing)}")

    con = duckdb.connect(DB)  # read-write: this is the *only* writer
    try:
        for key, table in MART_TABLES.items():
            df = outputs[key].copy()
            if "month" in df.columns:
                df["month"] = df["month"].astype(str)
            con.register("_df", df)
            con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM _df")
            con.unregister("_df")
            if verbose:
                print(f"materialized {table:34s} rows={len(df)}")

        h = build_hash()
        con.execute(
            f"CREATE OR REPLACE TABLE {META_TABLE} "
            "(build_hash VARCHAR, built_at_utc TIMESTAMP, n_tables INTEGER)"
        )
        con.execute(
            f"INSERT INTO {META_TABLE} VALUES (?, ?, ?)",
            [h, datetime.now(timezone.utc).replace(tzinfo=None), len(MART_TABLES)],
        )
    finally:
        con.close()

    if verbose:
        print(f"\nbuild_hash = {h}")
    return h


def ensure_ready(verbose: bool = False) -> str:
    """Build the database and marts if they are absent or stale.

    WHY THIS EXISTS
    ---------------
    ``data/processed/*.duckdb`` is gitignored, correctly: it is a build
    artifact derived from the committed CSVs, not source. But that means a
    fresh checkout -- a clone, CI, or a Streamlit Community Cloud deploy --
    has no database at all, and the agent opens it READ-ONLY, which cannot
    create one. The deployed app failed with "database does not exist" on
    every agent page while every other page worked, because the others compute
    from the CSVs in memory and never touch DuckDB.

    Materialization already knows how to build from source; it simply was
    never invoked on a machine that had never run it. This is the entry point
    that makes the agent self-healing on a cold start.

    Safe and cheap to call repeatedly: when the marts are present and current
    it is one hash comparison and returns.
    """
    try:
        con = duckdb.connect(DB, read_only=True)
        try:
            assert_fresh(con)
            return stored_hash(con) or ""
        finally:
            con.close()
    except (duckdb.Error, StaleMartError, OSError):
        # Absent, unreadable, or stale -- all are answered the same way, by
        # rebuilding from the committed CSVs.
        pass

    os.makedirs(os.path.dirname(DB), exist_ok=True)
    return materialize(verbose=verbose)


def connect_readonly() -> "duckdb.DuckDBPyConnection":
    """Read-only handle for the agent.

    Read-only is enforced at the driver, not by convention. In production this
    would be a least-privilege database role rather than a connection flag; the
    flag is the local stand-in and the security property is identical from the
    agent's side -- there is no code path through which it can write.
    """
    return duckdb.connect(DB, read_only=True)


if __name__ == "__main__":
    materialize()
