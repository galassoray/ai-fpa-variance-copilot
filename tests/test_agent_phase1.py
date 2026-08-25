"""
test_agent_phase1.py
====================
Phase-1 acceptance gate for the close-cycle agent's bounded tool surface.

Two families of test, and both have to be green before a model is allowed
anywhere near this registry.

PARITY -- the marts must agree with the canonical computation.
  The whole materialization design rests on the claim that SQL only slices
  figures pandas already computed. That claim is worth exactly as much as the
  test that enforces it. Every parity test below recomputes from
  ``run_pipeline.compute(load())`` in memory and asserts the tool returns the
  same figure to the cent. If someone later "optimizes" a tool by recomputing
  in SQL, these fail.

CONTAINMENT -- the surface must be bounded in the ways claimed.
  Read-only, no financial parameters, no silent coercion, no unknown tools, no
  extra arguments, no fuzzy dimension matching, and EMPTY distinguished from
  error. These are the properties a security reviewer is told about, so they
  are asserted rather than asserted-to.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, SRC)

import duckdb  # noqa: E402

import run_pipeline as rp  # noqa: E402
from agent import materialize as mz  # noqa: E402
from agent import registry as reg  # noqa: E402
from agent import tools as _tools  # noqa: E402,F401  (import registers the tools)

PERIOD = "2025-09"
PERIOD_KEY = "2025-09-01"
CENT = 0.011  # tolerance: figures are rounded to cents on both sides


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def outputs():
    """Canonical pandas computation, in memory."""
    return rp.compute(rp.load())


@pytest.fixture(scope="module")
def con(outputs):
    """Read-only handle to freshly materialized marts."""
    mz.materialize(outputs, verbose=False)
    c = mz.connect_readonly()
    yield c
    c.close()


def _call(name, params, con):
    return reg.call(name, params, con)


# --------------------------------------------------------------------------
# containment: the surface is what we say it is
# --------------------------------------------------------------------------
def test_registry_is_populated():
    assert len(reg.REGISTRY) >= 10, "tool registry did not load"


def test_no_tool_accepts_a_financial_parameter():
    """The structural guarantee: a model can never supply a figure.

    Enforced by type, not by review. If someone adds a float/money parameter,
    this fails -- which is the point, because that is the back door through
    which fabrication re-enters an otherwise clean pipeline.
    """
    for name, t in reg.REGISTRY.items():
        for pname, spec in t.params.items():
            assert isinstance(spec, reg.ALLOWED_PARAM_TYPES), (
                f"{name}.{pname} is {type(spec).__name__}, not a permitted parameter type"
            )
            assert not isinstance(spec, reg.ParamSpec) or spec.json_type in ("string", "integer"), (
                f"{name}.{pname} exposes json_type {spec.json_type}; "
                "financial quantities may not be tool parameters"
            )


def test_connection_is_read_only(con):
    """Read-only is enforced at the driver, not by convention."""
    with pytest.raises(duckdb.Error):
        con.execute("CREATE TABLE agent_should_not_be_able_to_do_this (x INTEGER)")
    with pytest.raises(duckdb.Error):
        con.execute("DELETE FROM out_variance_detail")


def test_unknown_tool_is_rejected(con):
    r = _call("run_sql", {"query": "SELECT 1"}, con)
    assert r.outcome == reg.INVALID_PARAM
    assert "no tool named" in r.error
    assert "get_pl_summary" in r.valid_alternatives


def test_unknown_parameter_is_rejected(con):
    """No extra-argument escape hatch. A surface with one is not bounded."""
    r = _call("get_pl_summary", {"period": PERIOD, "limit": 999}, con)
    assert r.outcome == reg.INVALID_PARAM
    assert "limit" in r.error


def test_missing_required_parameter_is_rejected(con):
    r = _call("get_pl_summary", {}, con)
    assert r.outcome == reg.INVALID_PARAM
    assert "period" in r.error


def test_invalid_dimension_member_returns_alternatives(con):
    """A nonexistent department is a retrieved fact with the real list attached."""
    r = _call("decompose_variance", {"period": PERIOD, "department_id": "LEGAL"}, con)
    assert r.outcome == reg.INVALID_PARAM
    assert "SM" in r.valid_alternatives and "RND" in r.valid_alternatives


def test_no_fuzzy_dimension_matching(con):
    """Case-insensitive rescue only. A near-miss must not silently resolve."""
    assert _call("decompose_variance", {"period": PERIOD, "department_id": "sm"}, con).ok
    r = _call("decompose_variance", {"period": PERIOD, "department_id": "S&M"}, con)
    assert r.outcome == reg.INVALID_PARAM


def test_out_of_range_int_is_rejected_not_clamped(con):
    """Silent coercion would hide a planning error. Eval target for this is zero."""
    r = _call("rank_variance_drivers", {"period": PERIOD, "dimension": "department", "top_n": 500}, con)
    assert r.outcome == reg.INVALID_PARAM
    assert "range" in r.error
    ok = _call("rank_variance_drivers", {"period": PERIOD, "dimension": "department", "top_n": 10}, con)
    assert ok.ok and ok.row_count <= 10


def test_nonexistent_period_is_rejected_with_alternatives(con):
    r = _call("get_pl_summary", {"period": "2029-03"}, con)
    assert r.outcome == reg.INVALID_PARAM
    assert PERIOD_KEY in r.valid_alternatives


def test_malformed_period_is_rejected(con):
    for bad in ["September 2025", "2025/09", "09-2025", ""]:
        assert _call("get_pl_summary", {"period": bad}, con).outcome == reg.INVALID_PARAM


def test_period_normalizes_both_accepted_formats(con):
    a = _call("get_pl_summary", {"period": "2025-09"}, con)
    b = _call("get_pl_summary", {"period": "2025-09-01"}, con)
    assert a.ok and b.ok and a.rows == b.rows


def test_empty_is_distinguished_from_error(con):
    """A valid query with no rows is a fact, and the model is told so."""
    r = _call("get_pl_summary", {"period": "2024-03", "comparison": "actual_vs_forecast"}, con)
    assert r.outcome == reg.EMPTY, "2024 has no forecast; this must be EMPTY, not an error"
    view = r.to_model_view()
    assert "not an error" in view["note"]


def test_forecast_comparison_works_at_every_grain(con):
    """Regression: the pre-rolled pandas marts only carry actual-vs-budget
    columns, so sourcing rollups from them silently made actual_vs_forecast
    unavailable above the detail grain -- the tool raised instead of returning
    a forecast comparison. Rollups now aggregate from the detail mart."""
    for params in [
        {"period": PERIOD, "comparison": "actual_vs_forecast"},
    ]:
        assert _call("get_pl_summary", params, con).ok
    for dim in ["department", "statement_line", "account", "account_category"]:
        r = _call("rank_variance_drivers",
                  {"period": PERIOD, "dimension": dim, "comparison": "actual_vs_forecast"}, con)
        assert r.ok, f"{dim} -> {r.outcome} {r.error}"
    assert _call("decompose_variance",
                 {"period": PERIOD, "department_id": "SM",
                  "comparison": "actual_vs_forecast"}, con).ok


def test_forecast_rollup_matches_detail_sum(con):
    """The forecast rollup has no pandas counterpart to check against, so it is
    pinned against the sum of the detail grain it aggregates."""
    summary = _call("get_pl_summary",
                    {"period": PERIOD, "comparison": "actual_vs_forecast"}, con).rows
    total = sum(r["oi_impact"] for r in summary)
    detail = _call("rank_variance_drivers",
                   {"period": PERIOD, "dimension": "department",
                    "comparison": "actual_vs_forecast", "top_n": 10}, con).rows
    assert abs(total - sum(r["oi_impact"] for r in detail)) < 1.0


def test_trend_window_is_bounded(con):
    r = _call("get_trend", {"metric": "opex", "start_period": "2024-01", "end_period": "2025-12"}, con)
    assert r.ok and r.row_count == 24
    r2 = _call("get_trend", {"metric": "opex", "start_period": "2025-09", "end_period": "2025-01"}, con)
    assert r2.outcome == reg.TOOL_ERROR and "after" in r2.error


def test_resolved_defaults_are_recorded(con):
    """The ledger must show what actually ran, not what was typed."""
    r = _call("rank_variance_drivers", {"period": PERIOD, "dimension": "department"}, con)
    assert r.params_resolved["comparison"] == "actual_vs_budget"
    assert r.params_resolved["top_n"] == 5


def test_model_view_declares_truncation(con):
    r = _call("list_periods", {}, con)
    view = r.to_model_view(max_rows=5)
    assert len(view["rows"]) == 5 and "24 rows" in view["note"]


def test_all_tools_emit_valid_schemas():
    for s in reg.tool_schemas():
        assert s["name"] and s["description"]
        assert s["input_schema"]["additionalProperties"] is False
        for pname, p in s["input_schema"]["properties"].items():
            assert p["type"] in ("string", "integer"), f"{s['name']}.{pname}"


def test_every_tool_result_is_json_serializable(con):
    """A date or Decimal leaking through would fail mid-run, after success."""
    import json

    cases = [
        ("list_periods", {}),
        ("list_dimensions", {"dimension": "department"}),
        ("get_pl_summary", {"period": PERIOD}),
        ("get_operating_metrics", {"period": PERIOD}),
        ("rank_variance_drivers", {"period": PERIOD, "dimension": "account"}),
        ("decompose_variance", {"period": PERIOD, "department_id": "SM"}),
        ("get_comp_decomposition", {"period": PERIOD}),
        ("get_revenue_decomposition", {"period": PERIOD}),
        ("get_arr_bridge", {"period": PERIOD}),
        ("get_headcount_movement", {"period": PERIOD}),
        ("get_trend", {"metric": "revenue", "start_period": "2025-01", "end_period": "2025-09"}),
        ("compare_periods", {"period_a": PERIOD, "period_b": "2025-08",
                             "dimension": "department"}),
        ("get_ytd_summary", {"period": PERIOD}),
        ("rank_persistent_drivers", {"period": PERIOD, "dimension": "account"}),
        ("get_account_trend", {"account_id": "SM_SAL",
                               "start_period": "2025-04", "end_period": PERIOD}),
        ("get_opex_ratio_trend", {"department_id": "SM",
                                  "start_period": "2025-04", "end_period": PERIOD}),
        ("rank_mom_movers", {"period": PERIOD, "dimension": "account"}),
    ]
    assert {c[0] for c in cases} == set(reg.REGISTRY), "a tool is not covered by this test"
    for name, params in cases:
        r = _call(name, params, con)
        assert r.ok, f"{name} -> {r.outcome} {r.error}"
        json.dumps(r.to_model_view())


# --------------------------------------------------------------------------
# parity: SQL slices, it does not recompute
# --------------------------------------------------------------------------
def test_pl_summary_matches_computation(con, outputs):
    got = {r["statement_line"]: r for r in _call("get_pl_summary", {"period": PERIOD}, con).rows}
    exp = outputs["variance_by_statement_line"]
    exp = exp[exp["month"] == PERIOD_KEY]
    assert len(got) == len(exp)
    for _, e in exp.iterrows():
        g = got[e["statement_line"]]
        assert abs(g["actual"] - e["actual"]) < CENT
        assert abs(g["base"] - e["budget"]) < CENT
        assert abs(g["variance"] - e["var_ab_amount"]) < CENT
        assert abs(g["oi_impact"] - e["oi_impact_ab"]) < CENT
        assert g["favorable"] == bool(e["favorable_ab"])


def test_department_ranking_matches_computation(con, outputs):
    rows = _call("rank_variance_drivers",
                 {"period": PERIOD, "dimension": "department", "top_n": 5}, con).rows
    exp = outputs["variance_by_department"]
    exp = exp[exp["month"] == PERIOD_KEY].copy()
    exp["abs_oi"] = exp["oi_impact_ab"].abs()
    exp = exp.sort_values(["abs_oi", "department_id"], ascending=[False, True])

    assert [r["member"] for r in rows] == list(exp["department_id"])[:5], "ranking order diverged"
    for r in rows:
        e = exp[exp["department_id"] == r["member"]].iloc[0]
        assert abs(r["oi_impact"] - e["oi_impact_ab"]) < CENT
        assert abs(r["variance"] - e["var_ab_amount"]) < CENT


def test_ranking_uses_oi_impact_not_raw_variance(con, outputs):
    """The highest-risk defect in the whole build.

    Ranking on raw variance presents an expense overrun as a favorable result,
    and every downstream guardrail agrees because the figure itself is correct.
    This asserts the ordering is by |oi_impact| and that favorability follows
    the operating-income sign, not the raw one.
    """
    rows = _call("rank_variance_drivers",
                 {"period": PERIOD, "dimension": "account", "top_n": 10}, con).rows
    impacts = [abs(r["oi_impact"]) for r in rows]
    assert impacts == sorted(impacts, reverse=True), "not ranked by |oi_impact|"
    for r in rows:
        assert r["favorable"] == (r["oi_impact"] > 0)

    # An expense line over budget must be unfavorable even though variance > 0.
    over = [r for r in rows if r["variance"] > 0 and r["oi_impact"] < 0]
    assert over, "expected at least one over-budget expense line in the top 10"
    assert all(r["favorable"] is False for r in over)


def test_decomposition_matches_computation(con, outputs):
    rows = _call("decompose_variance",
                 {"period": PERIOD, "department_id": "SM", "top_n": 10}, con).rows
    line = outputs["variance_detail"]
    exp = line[(line["month"] == PERIOD_KEY) & (line["department_id"] == "SM")]
    lookup = {r["account_id"]: r for _, r in exp.iterrows()}
    for r in rows:
        e = lookup[r["account_id"]]
        assert abs(r["oi_impact"] - e["oi_impact_ab"]) < CENT
        assert abs(r["actual"] - e["actual"]) < CENT


def test_decomposition_shares_sum_to_one(con):
    """share_of_parent is a SQL window function, so the model never divides."""
    rows = _call("decompose_variance",
                 {"period": PERIOD, "department_id": "SM", "top_n": 10}, con).rows
    total = sum(r["share_of_department_oi_impact"] for r in rows if r["share_of_department_oi_impact"])
    assert 0 < total <= 1.0001


def test_arr_bridge_matches_and_ties(con, outputs):
    r = _call("get_arr_bridge", {"period": PERIOD}, con).rows[0]
    e = outputs["saas_metrics_summary"]
    e = e[e["month"] == PERIOD_KEY].iloc[0]
    for tool_col, exp_col in [
        ("starting_arr", "starting_arr"), ("new_arr", "new_arr"),
        ("expansion_arr", "expansion_arr"), ("contraction_arr", "contraction_arr"),
        ("churned_arr", "churned_arr"), ("ending_arr", "ending_arr"),
    ]:
        assert abs(r[tool_col] - e[exp_col]) < CENT, tool_col
    walk = (r["starting_arr"] + r["new_arr"] + r["expansion_arr"]
            - r["contraction_arr"] - r["churned_arr"])
    assert abs(walk - r["ending_arr"]) < 1.0, "ARR bridge does not tie"


def test_operating_metrics_match_computation(con, outputs):
    r = _call("get_operating_metrics", {"period": PERIOD}, con).rows[0]
    e = outputs["operating_metrics"]
    e = e[e["month"] == PERIOD_KEY].iloc[0]
    for col in ["revenue", "cogs", "opex", "gross_profit", "operating_income"]:
        assert abs(r[col] - e[col]) < CENT, col
    assert r["total_headcount"] == int(e["total_headcount"])


def test_comp_decomposition_matches_and_ties(con, outputs):
    rows = _call("get_comp_decomposition", {"period": PERIOD}, con).rows
    e = outputs["comp_decomposition"]
    e = e[e["month"] == PERIOD_KEY]
    lookup = {r["department_id"]: r for _, r in e.iterrows()}
    for r in rows:
        x = lookup[r["department_id"]]
        assert abs(r["salary_variance"] - x["salary_variance"]) < CENT
        assert abs(r["hc_impact"] + r["rate_impact"] + r["decomp_residual"]
                   - r["salary_variance"]) < 1.0, "comp decomposition does not tie"


def test_trend_matches_computation(con, outputs):
    rows = _call("get_trend",
                 {"metric": "opex", "start_period": "2025-01", "end_period": "2025-09"}, con).rows
    e = outputs["operating_metrics"]
    e = e[(e["month"] >= "2025-01-01") & (e["month"] <= "2025-09-01")]
    assert len(rows) == len(e)
    for r, (_, x) in zip(rows, e.iterrows()):
        assert abs(r["value"] - x["opex"]) < CENT


def test_tools_return_canonical_entity_names(con, outputs):
    """Names must match the entity-audit whitelist, or the narrative layer's
    own guardrail will reject tokens the tools handed it."""
    from guardrails import entity_audit as ea

    allowed = set(ea.canonical_entity_names(rp.load()))
    rows = _call("rank_variance_drivers",
                 {"period": PERIOD, "dimension": "department", "top_n": 5}, con).rows
    for r in rows:
        assert r["name"] in allowed, f"{r['name']} is not a canonical entity name"


# --------------------------------------------------------------------------
# staleness
# --------------------------------------------------------------------------
def test_fresh_marts_pass_the_freshness_check(con):
    mz.assert_fresh(con)


def test_stale_marts_are_refused(con, tmp_path, monkeypatch):
    """Serving a stale mart would produce wrong numbers that pass every
    downstream audit. Refusing is the correct behavior."""
    monkeypatch.setattr(mz, "build_hash", lambda: "deadbeefdeadbeef")
    with pytest.raises(mz.StaleMartError) as exc:
        mz.assert_fresh(con)
    assert "stale" in str(exc.value)


def test_build_hash_is_line_ending_independent(tmp_path, monkeypatch):
    """Regression: the hash originally digested raw bytes, so a Windows clone
    with core.autocrlf fingerprinted differently from a Linux clone of the
    identical dataset. That made it useless as a cross-environment integrity
    signal and would produce a spurious mismatch -- indistinguishable from real
    staleness -- between a laptop and Streamlit Community Cloud.
    """
    import shutil

    def _norm(b: bytes, crlf: bool) -> bytes:
        # Normalize to LF first: the source checkout may already be CRLF (it is
        # on Windows), and a naive LF->CRLF replace would produce \r\r\n.
        lf = b.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        return lf.replace(b"\n", b"\r\n") if crlf else lf

    base_syn, base_src = mz.SYN, mz.SRC
    hashes = set()
    try:
        for csv_crlf, py_crlf in [(False, False), (True, True), (True, False), (False, True)]:
            root = tmp_path / f"v{int(csv_crlf)}{int(py_crlf)}"
            syn = root / "synthetic"
            syn.mkdir(parents=True)
            for f in os.listdir(base_syn):
                if f.endswith(".csv"):
                    b = open(os.path.join(base_syn, f), "rb").read()
                    (syn / f).write_bytes(_norm(b, csv_crlf))
            src = root / "src"
            shutil.copytree(base_src, src, dirs_exist_ok=True)
            for rel in mz.COMPUTATION_SOURCES:
                p = src / rel
                if p.exists():
                    b = p.read_bytes()
                    p.write_bytes(_norm(b, py_crlf))
            monkeypatch.setattr(mz, "SYN", str(syn))
            monkeypatch.setattr(mz, "SRC", str(src))
            hashes.add(mz.build_hash())
    finally:
        mz.SYN, mz.SRC = base_syn, base_src

    assert len(hashes) == 1, f"hash varies with line endings: {hashes}"


def test_build_hash_changes_when_computation_changes(tmp_path, monkeypatch):
    before = mz.build_hash()
    extra = os.path.join(SRC, "calculations", "variance_engine.py")
    monkeypatch.setattr(mz, "COMPUTATION_SOURCES", mz.COMPUTATION_SOURCES + [extra, extra])
    # Same file listed twice changes the digest input, standing in for an edit.
    assert mz.build_hash() != before


# --------------------------------------------------------------------------
# comparative and trend tools
# --------------------------------------------------------------------------
# Added because the original eleven all answered variations of "what happened
# this month against plan", so every question landed on the same handful of
# calls and model-authored plans looked canned. That was a TOOL SURFACE
# problem, not a prompt problem.
#
# Held to exactly the same standard as the originals: parity against the
# canonical pandas layer, oi_impact rather than raw variance, bounded windows,
# and declared output types.

def test_compare_periods_matches_computation(con, outputs):
    rows = _call("compare_periods",
                 {"period_a": PERIOD, "period_b": "2025-08",
                  "dimension": "department", "top_n": 10}, con).rows
    det = outputs["variance_detail"]
    a = det[det["month"] == PERIOD_KEY].groupby("department_id")["actual"].sum()
    b = det[det["month"] == "2025-08-01"].groupby("department_id")["actual"].sum()
    for r in rows:
        assert abs(r["actual_a"] - a[r["member"]]) < CENT
        assert abs(r["actual_b"] - b[r["member"]]) < CENT
        assert abs(r["change"] - (a[r["member"]] - b[r["member"]])) < CENT


def test_compare_periods_ranks_by_absolute_change(con):
    rows = _call("compare_periods",
                 {"period_a": PERIOD, "period_b": "2025-08",
                  "dimension": "account", "top_n": 10}, con).rows
    changes = [abs(r["change"]) for r in rows]
    assert changes == sorted(changes, reverse=True)


def test_ytd_summary_matches_computation(con, outputs):
    rows = _call("get_ytd_summary", {"period": PERIOD}, con).rows
    det = outputs["variance_detail"]
    sub = det[det["month"] == PERIOD_KEY]
    for r in rows:
        line = sub[sub["statement_line"] == r["statement_line"]]
        assert abs(r["actual_ytd"] - line["actual_ytd"].sum()) < CENT
        assert abs(r["budget_ytd"] - line["budget_ytd"].sum()) < CENT
        assert abs(r["oi_impact_ytd"] - line["oi_impact_ytd"].sum()) < CENT


def test_ytd_is_larger_than_the_single_month(con, outputs):
    """September YTD must exceed September alone -- the check that the tool is
    reading the cumulative columns rather than the monthly ones."""
    ytd = {r["statement_line"]: r["actual_ytd"]
           for r in _call("get_ytd_summary", {"period": PERIOD}, con).rows}
    month = {r["statement_line"]: r["actual"]
             for r in _call("get_pl_summary", {"period": PERIOD}, con).rows}
    for line, v in month.items():
        assert ytd[line] > v * 5, f"{line}: YTD {ytd[line]} vs month {v}"


def test_persistent_drivers_counts_unfavourable_months(con, outputs):
    rows = _call("rank_persistent_drivers",
                 {"period": PERIOD, "months_back": 6, "dimension": "account",
                  "top_n": 10}, con).rows
    det = outputs["variance_detail"]
    months = sorted(det["month"].unique())
    window = [m for m in months if m <= PERIOD_KEY][-6:]
    sub = det[det["month"].isin(window)]
    for r in rows:
        acct = sub[sub["account_id"] == r["member"]]
        unfav = acct.groupby("month")["oi_impact_ab"].sum()
        assert r["months_observed"] == acct["month"].nunique()
        assert r["months_unfavorable"] == int((acct["oi_impact_ab"] < 0).sum())
        assert abs(r["cumulative_oi_impact"] - acct["oi_impact_ab"].sum()) < CENT


def test_persistent_drivers_answers_a_different_question(con):
    """A line that misses every month is a different problem from one that
    missed once, and the ranking must reflect that rather than this month's
    size."""
    persistent = _call("rank_persistent_drivers",
                       {"period": PERIOD, "months_back": 6,
                        "dimension": "account", "top_n": 5}, con).rows
    this_month = _call("rank_variance_drivers",
                       {"period": PERIOD, "dimension": "account",
                        "top_n": 5}, con).rows
    assert all(r["months_unfavorable"] >= 1 for r in persistent)
    counts = [r["months_unfavorable"] for r in persistent]
    assert counts == sorted(counts, reverse=True)
    assert [r["member"] for r in persistent] != [r["member"] for r in this_month], (
        "persistent ranking should not simply reproduce this month's ranking"
    )


def test_account_trend_matches_computation(con, outputs):
    rows = _call("get_account_trend",
                 {"account_id": "SM_SAL", "start_period": "2025-04",
                  "end_period": PERIOD}, con).rows
    det = outputs["variance_detail"]
    sub = det[(det["account_id"] == "SM_SAL")
              & (det["month"] >= "2025-04-01") & (det["month"] <= PERIOD_KEY)]
    exp = sub.groupby("month")["actual"].sum()
    assert len(rows) == len(exp)
    for r in rows:
        assert abs(r["actual"] - exp[r["month"]]) < CENT


def test_account_trend_window_is_bounded(con):
    r = _call("get_account_trend",
              {"account_id": "SM_SAL", "start_period": "2024-01",
               "end_period": "2025-12"}, con)
    assert r.ok and r.row_count == 24
    bad = _call("get_account_trend",
                {"account_id": "SM_SAL", "start_period": PERIOD,
                 "end_period": "2025-01"}, con)
    assert bad.outcome == reg.TOOL_ERROR and "after" in bad.error


def test_opex_ratio_trend_matches_computation(con, outputs):
    rows = _call("get_opex_ratio_trend",
                 {"department_id": "SM", "start_period": "2025-04",
                  "end_period": PERIOD}, con).rows
    exp = outputs["dept_opex_pct_revenue"]
    exp = exp[(exp["department_id"] == "SM")
              & (exp["month"] >= "2025-04-01") & (exp["month"] <= PERIOD_KEY)]
    lookup = {r["month"]: r for _, r in exp.iterrows()}
    assert len(rows) == len(exp)
    for r in rows:
        e = lookup[r["month"]]
        assert abs(r["dept_opex"] - e["dept_opex"]) < CENT
        assert abs(r["dept_opex_pct_revenue"] - e["dept_opex_pct_revenue"]) < 1e-4


def test_mom_movers_matches_computation(con, outputs):
    rows = _call("rank_mom_movers",
                 {"period": PERIOD, "dimension": "account", "top_n": 10}, con).rows
    det = outputs["variance_detail"]
    sub = det[det["month"] == PERIOD_KEY]
    exp = sub.groupby("account_id")["actual_mom_amount"].sum()
    for r in rows:
        assert abs(r["change_vs_prior_month"] - exp[r["member"]]) < CENT
    changes = [abs(r["change_vs_prior_month"]) for r in rows]
    assert changes == sorted(changes, reverse=True)


def test_new_tools_return_operating_income_impact(con):
    """The sign-convention rule applies to every tool, not just the originals:
    a raw change is uninterpretable without knowing whether the line is revenue
    or expense."""
    for name, params in [
        ("compare_periods", {"period_a": PERIOD, "period_b": "2025-08",
                             "dimension": "department"}),
        ("rank_mom_movers", {"period": PERIOD, "dimension": "account"}),
    ]:
        rows = _call(name, params, con).rows
        assert rows and all("oi_impact_of_change" in r for r in rows)

    rev = [r for r in _call("rank_mom_movers",
                            {"period": PERIOD, "dimension": "account",
                             "top_n": 10}, con).rows
           if r["member"].startswith("REV_")]
    for r in rev:
        assert (r["oi_impact_of_change"] > 0) == (r["change_vs_prior_month"] > 0)


def test_the_new_tools_are_type_declared_and_bounded():
    for name in ("compare_periods", "get_ytd_summary", "rank_persistent_drivers",
                 "get_account_trend", "get_opex_ratio_trend", "rank_mom_movers"):
        t = reg.REGISTRY[name]
        assert t.field_types is not None, f"{name} declares no field_types"
        for pname, spec in t.params.items():
            assert isinstance(spec, reg.ALLOWED_PARAM_TYPES)
            if isinstance(spec, reg.IntParam):
                assert spec.hi <= 12, f"{name}.{pname} is unbounded"
