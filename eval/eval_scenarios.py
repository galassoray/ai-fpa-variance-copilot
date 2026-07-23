"""
eval_scenarios.py
=================
Builds the evaluation scenarios from the computed Phase-1 outputs: several real
reporting months (each with a full fact pack) plus one deliberately
insufficient-data scenario to exercise the refusal path.
"""
from __future__ import annotations
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import run_pipeline as rp
from scenario import Scenario
import generate_synthetic_data as gen
from narrative import fact_pack as fp

EVAL_MONTHS = ["2025-06-01", "2025-07-01", "2025-09-01", "2025-11-01", "2025-12-01"]


def load_context():
    """Build the BASELINE dataset in memory from the seeded generator.

    The eval must not depend on the DuckDB file: that file is a build artifact
    (gitignored, and stripped from the package), so a fresh clone or a cloud
    deployment would not have it. The baseline is *defined* by the default
    Scenario, so building it here makes the eval self-contained, deterministic,
    and identical whether it runs in CI, on a laptop, or in the deployed app.
    It is also why the eval headline is unaffected by scenario edits in the UI.
    """
    tables = gen.build_dataset(Scenario())
    outputs = rp.compute(tables)
    return tables, outputs


def build_scenarios():
    """Return list of {id, pack, kind}. kind in {'ok','insufficient'}."""
    tables, outputs = load_context()
    scenarios = []
    for m in EVAL_MONTHS:
        pack = fp.build_fact_pack(m, outputs, tables)
        scenarios.append({"id": f"month_{m}", "pack": pack, "kind": "ok"})
    # insufficient-data scenario (month with no computed rows) -> refusal path
    bad_pack = fp.build_fact_pack("2099-01-01", outputs, tables)
    scenarios.append({"id": "insufficient_2099", "pack": bad_pack, "kind": "insufficient"})
    return scenarios, tables, outputs
