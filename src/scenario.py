"""
scenario.py
===========
Every INPUT to the synthetic company in one place.

The distinction this module encodes is the one that makes live editing safe:

  * INPUTS  (here)  -- assumptions a planner actually sets: headcount plan,
                       average comp, marketing spend, churn, the story
                       multipliers. Nothing derives them.
  * DERIVED (elsewhere) -- salary lines (= headcount x comp/12), revenue
                       (= ARR bridge / 12), COGS (= % of revenue), and every
                       variance, rollup and decomposition on top.

Only inputs are editable. Derived values are always recomputed in code, so the
reconciliations that make the tool defensible (salary ties to headcount to the
penny; the ARR bridge closes; decompositions reconcile) hold under ANY scenario.

Changing a scenario re-runs the same seeded generator, so a change is
attributable to the input you moved and not to random noise.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict, replace
import json

DEPTS = ["SM", "RND", "GA", "CS"]


def _hc_start():
    return {"SM": 40, "RND": 38, "GA": 15, "CS": 18}


def _hc_end():
    return {"SM": 58, "RND": 52, "GA": 21, "CS": 27}


def _avg_comp():
    return {"SM": 145_000, "RND": 168_000, "GA": 150_000, "CS": 112_000}


def _comp_drift():
    return {"SM": 0.04, "RND": 0.05, "GA": 0.025, "CS": 0.02}


@dataclass
class Scenario:
    """All editable inputs. Defaults reproduce the baseline dataset exactly."""

    # ---- seed (kept fixed so a change is attributable to the input, not noise)
    seed: int = 20260625

    # ---- growth & retention (ARR bridge inputs) ----
    starting_arr: float = 21_000_000.0
    starting_customers: int = 480
    base_new_arr: float = 560_000.0
    new_arr_growth: float = 1_500.0
    base_expansion_arr: float = 170_000.0
    expansion_arr_growth: float = 500.0
    base_contraction_arr: float = 90_000.0
    contraction_arr_growth: float = 200.0
    base_churned_arr: float = 200_000.0
    churned_arr_growth: float = 700.0
    base_new_customers: int = 14
    base_churned_customers: int = 6

    # ---- FY2025 story multipliers on ACTUALS ----
    actual_new_arr_mult: float = 0.86      # bookings landed under plan
    actual_churn_mult: float = 1.22        # churn ran hot
    actual_new_cust_mult: float = 0.85
    actual_churn_cust_mult: float = 1.30
    # ---- FY2025 story multipliers on the REFORECAST ----
    forecast_new_arr_mult: float = 0.93
    forecast_churn_mult: float = 1.10
    forecast_new_cust_mult: float = 0.92
    forecast_churn_cust_mult: float = 1.15

    # ---- people ----
    hc_budget_start: dict = field(default_factory=_hc_start)
    hc_budget_end: dict = field(default_factory=_hc_end)
    avg_comp_budget: dict = field(default_factory=_avg_comp)
    comp_drift: dict = field(default_factory=_comp_drift)
    sm_ramp_early: int = 2                 # S&M hired ahead of plan (H1)
    sm_ramp_late: int = 4                  # S&M hired ahead of plan (H2)
    cs_hc_shortfall: int = 3               # CS could not backfill
    rnd_hc_shortfall: int = 2              # R&D hired slower than plan
    # what the reforecast acknowledged of the above
    sm_ramp_early_forecast: int = 1
    sm_ramp_late_forecast: int = 2
    cs_hc_shortfall_forecast: int = 2

    # ---- revenue mix / COGS rates ----
    services_pct_of_subscription: float = 0.07
    hosting_pct_of_revenue: float = 0.115
    support_pct_of_revenue: float = 0.045
    payments_pct_of_revenue: float = 0.018
    actual_hosting_mult: float = 1.18      # hosting cost pressure
    forecast_hosting_mult: float = 1.08

    # ---- S&M spend ----
    commission_pct_of_revenue: float = 0.06
    actual_commission_mult: float = 0.92
    base_marketing: float = 180_000.0
    marketing_growth: float = 1_200.0
    actual_marketing_mult: float = 1.20    # paid marketing overspend
    forecast_marketing_mult: float = 1.10
    base_events: float = 35_000.0
    events_growth: float = 300.0

    # ---- R&D spend ----
    base_contractors: float = 90_000.0
    contractors_growth: float = 600.0
    actual_contractors_mult: float = 1.55  # contractor overrun
    forecast_contractors_mult: float = 1.25

    # ---- G&A spend ----
    base_legal: float = 45_000.0
    legal_growth: float = 200.0
    actual_legal_spike_mult: float = 3.4   # Jun/Jul financing event
    forecast_legal_spike_mult: float = 1.6

    # ---- CS spend ----
    base_cs_software: float = 20_000.0
    cs_software_growth: float = 110.0
    actual_cs_software_mult: float = 1.30  # bought tooling instead of people
    actual_cs_training_mult: float = 1.18
    forecast_cs_software_mult: float = 1.12

    # ---- remaining flat spend lines (inputs; no story attached) ----
    base_sm_software: float = 22_000.0
    sm_software_growth: float = 80.0
    base_sm_te: float = 18_000.0
    sm_te_growth: float = 120.0
    base_rnd_cloud: float = 60_000.0
    rnd_cloud_growth: float = 400.0
    base_rnd_software: float = 28_000.0
    rnd_software_growth: float = 120.0
    base_rnd_recruiting: float = 25_000.0
    rnd_recruiting_growth: float = 150.0
    base_ga_software: float = 30_000.0
    ga_software_growth: float = 100.0
    base_ga_insurance: float = 22_000.0
    ga_insurance_growth: float = 60.0
    base_ga_facilities: float = 55_000.0
    ga_facilities_growth: float = 150.0
    base_ga_admin: float = 18_000.0
    ga_admin_growth: float = 80.0
    base_cs_training: float = 12_000.0
    cs_training_growth: float = 70.0
    base_cs_te: float = 10_000.0
    cs_te_growth: float = 60.0

    # ---------------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    def key(self) -> str:
        """Stable cache key for a scenario."""
        return self.to_json()

    def with_changes(self, **kw) -> "Scenario":
        return replace(self, **kw)

    def diff_from_default(self) -> dict:
        """Which inputs were changed from baseline (for the UI + decision trail)."""
        base = asdict(Scenario())
        mine = asdict(self)
        return {k: (base[k], mine[k]) for k in mine if mine[k] != base[k]}


DEFAULT = Scenario()
