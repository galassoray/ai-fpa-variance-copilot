"""
test_phase4.py
==============
Gates for scenario editing, the projection engine, and the guarded chat.

The load-bearing property: editing INPUTS can never break the reconciliations
that make the tool defensible. Every check must pass for ANY scenario, not just
the baseline -- otherwise "change the numbers live in an interview" is a trap.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import numpy as np
import pandas as pd

import scenario as SCEN
import generate_synthetic_data as gen
import run_pipeline as rp
from validation import validate_data as vd
from calculations import projection as PJ
from narrative import fact_index as FI
from narrative import chat as CH
from narrative.llm_client import ScriptedClient
from guardrails import entity_audit as ea

_BASE_T = gen.build_dataset(SCEN.Scenario())
_BASE_OUT = rp.compute(_BASE_T)
_NAMES = ea.canonical_entity_names(_BASE_T)

SCENARIOS = [
    ("baseline", SCEN.Scenario()),
    ("hot_spend", SCEN.Scenario().with_changes(actual_marketing_mult=1.6,
                                               actual_contractors_mult=2.0,
                                               actual_churn_mult=1.8)),
    ("big_company", SCEN.Scenario().with_changes(
        starting_arr=40_000_000.0,
        avg_comp_budget={"SM": 190_000.0, "RND": 220_000.0, "GA": 195_000.0, "CS": 145_000.0})),
    ("small_company", SCEN.Scenario().with_changes(
        starting_arr=3_000_000.0,
        hc_budget_end={"SM": 12, "RND": 10, "GA": 4, "CS": 5})),
    ("hiring_swing", SCEN.Scenario().with_changes(cs_hc_shortfall=8, sm_ramp_late=10)),
]


def test_default_scenario_matches_canonical_dataset():
    """The default Scenario must reproduce the dataset the portfolio is built on."""
    disk = rp.load()
    for name in ["fact_actuals", "fact_budget", "fact_headcount", "fact_saas_metrics"]:
        a = disk[name].reset_index(drop=True)
        b = _BASE_T[name].reset_index(drop=True)
        assert a.shape == b.shape, name
        num = [c for c in a.columns if pd.api.types.is_numeric_dtype(a[c])]
        diff = np.nanmax(np.abs(a[num].to_numpy(dtype=float)
                                - b[num].to_numpy(dtype=float)))
        assert diff <= 0.005, f"{name} drifted by {diff}"


def test_all_checks_pass_for_every_scenario():
    """Editing inputs must never break a reconciliation."""
    for name, sc in SCENARIOS:
        t = gen.build_dataset(sc)
        out = rp.compute(t)
        df = vd.run_checks(t, out)
        failed = list(df[df["status"] == "FAIL"]["check"])
        assert not failed, f"scenario {name} failed: {failed}"


def test_salary_still_ties_to_headcount_under_edits():
    """The tie that makes comp decomposition defensible survives any scenario."""
    for name, sc in SCENARIOS:
        t = gen.build_dataset(sc)
        hc = t["fact_headcount"].copy()
        hc["calc"] = (hc["actual_headcount"] * hc["avg_comp_actual"] / 12.0).round(2)
        sal = t["fact_actuals"][t["fact_actuals"]["account_id"].str.endswith("_SAL")]
        m = sal.merge(hc[["month", "department_id", "calc"]],
                      on=["month", "department_id"], how="left")
        assert (m["amount"] - m["calc"]).abs().max() <= 0.01, name


def test_changing_an_input_moves_the_derived_numbers():
    """A knob must actually propagate -- otherwise the demo is theatre."""
    sc = SCEN.Scenario().with_changes(actual_marketing_mult=1.60)
    out = rp.compute(gen.build_dataset(sc))
    q = "month=='2025-09-01' and account_id=='SM_MKT'"
    base = _BASE_OUT["variance_detail"].query(q)["actual"].iloc[0]
    moved = out["variance_detail"].query(q)["actual"].iloc[0]
    assert moved > base * 1.2


def test_scenario_is_deterministic():
    """Same inputs -> same numbers, every time."""
    sc = SCEN.Scenario().with_changes(actual_churn_mult=1.45)
    a = rp.compute(gen.build_dataset(sc))["variance_detail"]["actual"].to_numpy()
    b = rp.compute(gen.build_dataset(sc))["variance_detail"]["actual"].to_numpy()
    assert np.array_equal(a, b)


# ---- projection ----------------------------------------------------------
def _company_frame():
    bs = _BASE_OUT["variance_by_statement_line"]
    piv = bs.pivot_table(index="month", columns="statement_line",
                         values=["actual", "budget"], aggfunc="sum").fillna(0.0)
    df = pd.DataFrame({"month": piv.index})
    df["Revenue_actual"] = piv[("actual", "Revenue")].values
    df["Revenue_budget"] = piv[("budget", "Revenue")].values
    return df.sort_values("month").reset_index(drop=True)


def test_projection_methods_are_deterministic_and_forward_looking():
    cm = _company_frame()
    for method in ["linear_trend", "run_rate"]:
        p1 = PJ.project_series(cm, "Revenue_actual", method, horizon=6)
        p2 = PJ.project_series(cm, "Revenue_actual", method, horizon=6)
        assert p1.values == p2.values
        assert len(p1.values) == 6
        assert all(m > cm["month"].iloc[-1] for m in p1.months)
        assert p1.assumption


def test_run_rate_is_flat_and_linear_trend_is_not():
    cm = _company_frame()
    rr = PJ.project_series(cm, "Revenue_actual", "run_rate", horizon=6)
    assert len(set(rr.values)) == 1  # flat by construction
    lt = PJ.project_series(cm, "Revenue_actual", "linear_trend", horizon=6)
    assert len(set(lt.values)) > 1


def test_driver_projection_closes_the_arr_bridge():
    saas = _BASE_OUT["saas_metrics_summary"]
    p = PJ.project_revenue_driver(saas, horizon=6, lookback=3)
    assert len(p.values) == 6
    assert p.method == "driver_arr"
    # revenue is ARR/12, so successive months move by a constant net bridge
    deltas = np.diff(p.values)
    assert np.allclose(deltas, deltas[0], atol=0.02)


# ---- chat ----------------------------------------------------------------
_INDEX = FI.build_fact_index(_BASE_OUT, _BASE_T)


def test_fact_index_covers_whole_dataset():
    months = {r.month for r in _INDEX}
    assert len(months) == 24
    assert {"SM", "RND", "GA", "CS", "CORP"} <= {r.department for r in _INDEX}


def test_slice_whitelist_is_tighter_than_whole_index():
    """The reason for slicing: a tight whitelist keeps 'verified' meaningful."""
    pack, rows, note = CH.build_chat_pack(
        "What was paid marketing in September 2025?", _INDEX, "2025-09-01", _NAMES)
    slice_n = len(pack.allowed_by_kind("dollar"))
    whole_n = len([r for r in _INDEX if r.kind == "dollar"])
    assert slice_n < whole_n / 5


def test_chat_accepts_clean_answer_and_refuses_fabrication():
    q = "What was paid marketing in September 2025?"
    pack, rows, _ = CH.build_chat_pack(q, _INDEX, "2025-09-01", _NAMES)
    real = next(a.value for a in pack.allowed_by_kind("dollar")
                if "Paid Marketing" in a.label and "actual" in a.label)
    good = ScriptedClient([f"Paid Marketing was ${real:,.2f} in September 2025."])
    ok = CH.answer_question(q, _INDEX, "2025-09-01", good, _NAMES)
    assert ok.status == "accepted"

    bad_text = "Paid Marketing was $9,918,273.45 in September 2025."
    bad = ScriptedClient([bad_text, bad_text])
    refused = CH.answer_question(q, _INDEX, "2025-09-01", bad, _NAMES)
    assert refused.status == "refused"
    assert sum(refused.violations_caught) >= 1


def test_chat_never_returns_unaudited_text():
    """Whatever the chat returns has passed the audit or is the refusal."""
    q = "How did S&M spend trend in 2025?"
    bad = "S&M spent $12,345,678.90 which is 61.2% over plan."
    client = ScriptedClient([bad, bad])
    ans = CH.answer_question(q, _INDEX, "2025-09-01", client, _NAMES)
    assert ans.status == "refused"
    assert ans.text == CH.REFUSAL


# ---- regression -----------------------------------------------------------
def test_eval_does_not_depend_on_the_database_file(monkeypatch):
    """The Guardrails page crashed on a fresh install because the eval read the
    DuckDB file -- a build artifact that is gitignored, stripped from the
    package, and therefore absent on a fresh extraction AND on a cloud deploy.
    The eval must build the baseline from the seeded generator instead.
    """
    sys.path.insert(0, os.path.join(HERE, "..", "eval"))
    import eval_scenarios as ES

    def boom(*a, **k):
        raise AssertionError("the eval must not read the DuckDB file")

    monkeypatch.setattr(rp, "load", boom)
    scenarios, tables, outputs = ES.build_scenarios()
    assert len(scenarios) >= 2
    assert "variance_detail" in outputs
    assert not outputs["variance_detail"].empty


def test_eval_baseline_matches_the_canonical_dataset():
    """The in-memory eval baseline must be the same company as the packaged one."""
    sys.path.insert(0, os.path.join(HERE, "..", "eval"))
    import eval_scenarios as ES
    _, tables, _ = ES.build_scenarios()
    a = tables["fact_actuals"].reset_index(drop=True)
    b = _BASE_T["fact_actuals"].reset_index(drop=True)
    assert a.shape == b.shape
    assert np.allclose(a["amount"].to_numpy(dtype=float),
                       b["amount"].to_numpy(dtype=float), atol=0.005)
