"""
fact_pack.py
============
Builds the *fact pack*: the structured, numbers-are-already-computed payload the
LLM is allowed to see for one reporting period, plus the whitelist of every value
the model is permitted to state (the "allowed set" the numeric audit enforces).

The fact pack is 100% deterministic. The LLM never computes anything; it only
narrates the contents of this object. Nothing here calls a model.

Two things leave this module:
  * `FactPack.prompt_facts` — a compact, labeled dict handed to the LLM.
  * `FactPack.allowed_values` — list[AllowedValue] the audit checks output against.
  * `FactPack.allowed_entities` — department/account names in scope.
  * `FactPack.status` — "ok" or "insufficient_data" (drives the refusal path).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal
import pandas as pd

from calculations import driver_decomposition as dd

Kind = Literal["dollar", "percent", "count"]


@dataclass(frozen=True)
class AllowedValue:
    value: float          # canonical numeric value (dollars, ratio, or count)
    kind: Kind
    label: str            # human-readable provenance, e.g. "S&M Paid Marketing variance"


@dataclass
class FactPack:
    month: str
    scope: str
    status: str                       # "ok" | "insufficient_data"
    reason: str = ""                  # populated when insufficient
    prompt_facts: dict = field(default_factory=dict)
    allowed_values: list = field(default_factory=list)
    allowed_entities: list = field(default_factory=list)

    def allowed_by_kind(self, kind: Kind) -> list:
        return [a for a in self.allowed_values if a.kind == kind]


def _add(bag: list, value, kind: Kind, label: str):
    """Add a value plus its absolute value (the model speaks magnitudes)."""
    if value is None or pd.isna(value):
        return
    v = float(value)
    bag.append(AllowedValue(v, kind, label))
    if v < 0:
        bag.append(AllowedValue(abs(v), kind, label + " (magnitude)"))


def build_fact_pack(month: str, outputs: dict, tables: dict,
                    scope: str = "company", top_n: int = 6) -> FactPack:
    """Assemble the fact pack for a month from the computed Phase-1 outputs."""
    line = outputs["variance_detail"]
    op = outputs["operating_metrics"]
    saas = outputs["saas_metrics_summary"]
    by_dept = outputs["variance_by_department"]

    m_line = line[line["month"] == month]
    if m_line.empty:
        return FactPack(month, scope, "insufficient_data",
                        reason=f"no computed variance rows for {month}")

    allowed: list = []
    entities: list = []

    # ---- company P&L headline ----
    opm = op[op["month"] == month]
    facts: dict = {"period": month, "scope": scope}
    if not opm.empty:
        r = opm.iloc[0]
        facts["company"] = {
            "revenue_actual": _money(r["revenue"]),
            "gross_margin": _pct(r["gross_margin"]),
            "operating_income_actual": _money(r["operating_income"]),
            "operating_margin": _pct(r["operating_margin"]),
        }
        _add(allowed, r["revenue"], "dollar", "company revenue (actual)")
        _add(allowed, r["operating_income"], "dollar", "operating income (actual)")
        _add(allowed, r["gross_margin"], "percent", "gross margin")
        _add(allowed, r["operating_margin"], "percent", "operating margin")

    # ---- revenue vs budget (from the CORP subscription+services lines) ----
    rev_rows = m_line[m_line["is_revenue"] == 1]
    if not rev_rows.empty:
        ra, rb = rev_rows["actual"].sum(), rev_rows["budget"].sum()
        rvar = round(ra - rb, 2)
        facts["revenue_vs_budget"] = {
            "actual": _money(ra), "budget": _money(rb), "variance": _money(rvar),
            "favorable": bool(rvar > 0),
        }
        _add(allowed, ra, "dollar", "revenue actual")
        _add(allowed, rb, "dollar", "revenue budget")
        _add(allowed, rvar, "dollar", "revenue variance")

    # ---- ARR bridge + retention ----
    sm_row = saas[saas["month"] == month]
    if not sm_row.empty:
        s = sm_row.iloc[0]
        facts["arr"] = {
            "ending_arr": _money(s["ending_arr"]), "starting_arr": _money(s["starting_arr"]),
            "new_arr": _money(s["new_arr"]), "expansion_arr": _money(s["expansion_arr"]),
            "contraction_arr": _money(s["contraction_arr"]), "churned_arr": _money(s["churned_arr"]),
            "nrr_ttm": _pct(s.get("nrr_ttm")), "grr_ttm": _pct(s.get("grr_ttm")),
        }
        for col, lbl, kind in [
            ("ending_arr", "ending ARR", "dollar"), ("starting_arr", "starting ARR", "dollar"),
            ("new_arr", "new ARR", "dollar"), ("expansion_arr", "expansion ARR", "dollar"),
            ("contraction_arr", "contraction ARR", "dollar"), ("churned_arr", "churned ARR", "dollar"),
            ("nrr_ttm", "NRR (ttm)", "percent"), ("grr_ttm", "GRR (ttm)", "percent"),
        ]:
            _add(allowed, s.get(col), kind, lbl)

    # ---- top drivers (materiality-ranked) ----
    drivers = dd.top_drivers(line, month, n=top_n)
    driver_list = []
    for _, d in drivers.iterrows():
        driver_list.append({
            "line_item": d["account_name"], "department": d["department_id"],
            "actual": _money(d["actual"]), "budget": _money(d["budget"]),
            "variance": _money(d["var_ab_amount"]),
            "pct_variance": _pct(d["var_ab_pct"]),
            "favorable": bool(d["favorable_ab"]),
            "oi_impact": _money(d["oi_impact_ab"]),
        })
        entities.append(d["account_name"])
        _add(allowed, d["actual"], "dollar", f"{d['account_name']} actual")
        _add(allowed, d["budget"], "dollar", f"{d['account_name']} budget")
        _add(allowed, d["var_ab_amount"], "dollar", f"{d['account_name']} variance")
        _add(allowed, d["oi_impact_ab"], "dollar", f"{d['account_name']} OI impact")
        _add(allowed, d["var_ab_pct"], "percent", f"{d['account_name']} % variance")
    facts["top_drivers"] = driver_list

    # ---- department rollups in scope ----
    depts_in = sorted({d["department"] for d in driver_list} | {"SM", "RND", "GA", "CS"})
    dep_rows = by_dept[(by_dept["month"] == month) & (by_dept["department_id"].isin(depts_in))]
    dept_summ = []
    for _, d in dep_rows.iterrows():
        dept_summ.append({"department": d["department_id"], "actual": _money(d["actual"]),
                          "budget": _money(d["budget"]), "variance": _money(d["var_ab_amount"]),
                          "favorable": bool(d["favorable_ab"])})
        _add(allowed, d["actual"], "dollar", f"{d['department_id']} opex actual")
        _add(allowed, d["budget"], "dollar", f"{d['department_id']} opex budget")
        _add(allowed, d["var_ab_amount"], "dollar", f"{d['department_id']} opex variance")
    facts["departments"] = dept_summ

    # ---- comp decomposition (headcount vs rate) for salary-driver depts ----
    comp = dd.comp_decomposition(tables["fact_headcount"])
    comp_m = comp[comp["month"] == month]
    comp_out = []
    for _, c in comp_m.iterrows():
        if abs(c["salary_variance"]) < 100:
            continue
        comp_out.append({
            "department": c["department_id"],
            "salary_variance": _money(c["salary_variance"]),
            "headcount_effect": _money(c["hc_impact"]),
            "rate_effect": _money(c["rate_impact"]),
        })
        _add(allowed, c["salary_variance"], "dollar", f"{c['department_id']} salary variance")
        _add(allowed, c["hc_impact"], "dollar", f"{c['department_id']} headcount effect")
        _add(allowed, c["rate_impact"], "dollar", f"{c['department_id']} rate effect")
    facts["comp_decomposition"] = comp_out

    # ---- revenue decomposition (volume vs price) ----
    rev_dec = dd.revenue_decomposition(tables["fact_saas_metrics"], tables["fact_saas_metrics_budget"])
    rd = rev_dec[rev_dec["month"] == month]
    if not rd.empty:
        r = rd.iloc[0]
        facts["revenue_decomposition"] = {
            "variance": _money(r["rev_variance"]),
            "volume_effect": _money(r["volume_impact"]),
            "price_effect": _money(r["price_impact"]),
        }
        _add(allowed, r["rev_variance"], "dollar", "subscription revenue variance")
        _add(allowed, r["volume_impact"], "dollar", "revenue volume effect")
        _add(allowed, r["price_impact"], "dollar", "revenue price effect")

    # ---- headcount vs plan for depts in scope ----
    hcp = outputs["headcount_vs_plan"]
    hcp_m = hcp[(hcp["month"] == month) & (hcp["department_id"].isin(depts_in))]
    hc_out = []
    for _, h in hcp_m.iterrows():
        hc_out.append({"department": h["department_id"],
                       "actual_headcount": int(h["actual_headcount"]),
                       "budget_headcount": int(h["budget_headcount"]),
                       "hc_vs_plan": int(h["hc_var_vs_budget"])})
        _add(allowed, h["actual_headcount"], "count", f"{h['department_id']} headcount actual")
        _add(allowed, h["budget_headcount"], "count", f"{h['department_id']} headcount budget")
        _add(allowed, h["hc_var_vs_budget"], "count", f"{h['department_id']} headcount vs plan")
    facts["headcount"] = hc_out

    # entity whitelist: department names + account names in scope
    dep_names = tables["dim_department"].set_index("department_id")["department_name"].to_dict()
    entities += [dep_names.get(d, d) for d in depts_in]
    entities = sorted(set(entities))

    return FactPack(month, scope, "ok", prompt_facts=facts,
                    allowed_values=allowed, allowed_entities=entities)


# ---- formatting helpers (display only; audit uses the raw allowed_values) ----
def _money(x):
    if x is None or pd.isna(x):
        return None
    return round(float(x), 2)


def _pct(x):
    if x is None or pd.isna(x):
        return None
    return round(float(x), 5)
