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

    # ---- bookings seasonality (OFF by default) ----
    # Recognized revenue is derived as ending ARR / 12, and ARR is a STOCK.
    # Monthly noise on the FLOWS (~3% of ~$500K of new ARR) is diluted roughly
    # 60:1 against a $26M ARR base, so it moves revenue by ~0.06%. The result is
    # a revenue series that is very nearly a straight line: trailing-12 R^2 of
    # 0.9996, month-over-month growth confined to 0.99%-1.28%. High R^2 on
    # subscription revenue is realistic -- that smoothness is the point of the
    # model -- but a fit that round is not, and it invites the question of
    # whether the data was generated on a line.
    #
    # Seasonality is the honest missing ingredient: real SaaS bookings peak into
    # Q4 and trough over the summer, and renewal dates cluster. Applying it to
    # the FLOWS keeps revenue == ARR / 12 exactly, so the ARR bridge still
    # closes, the volume/price decomposition still ties to the P&L, and all 11
    # validation checks still pass. Nothing downstream has to change.
    #
    # Default is 0.0, which reproduces the canonical dataset bit-for-bit.
    # Measured at amplitude 0.25 / 0.30: trailing-12 R^2 0.9999 -> ~0.98,
    # MoM growth 1.49%-1.97% -> 0.88%-2.39%, ending ARR effectively unchanged.
    bookings_seasonality: float = 0.0      # amplitude on new + expansion ARR
    churn_seasonality: float = 0.0         # renewal-cycle clustering on churn

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

    # ---- FY2024 story multipliers on ACTUALS (OFF by default) ----
    # FY2024 currently carries no story at all: actual-vs-budget revenue
    # variance averages 0.16% and every opex line tracks plan, so the first
    # twelve rows of any variance view read "actual == budget, every month".
    # A clean comparison year is defensible, but it spends twelve rows of an
    # interviewer's attention proving the engine has nothing to find.
    #
    # The story is deliberately (a) opex-only, (b) H2-weighted, and (c)
    # two-sided. Opex-only because FY2024 revenue tracking plan is the honest
    # setup for the FY2025 bookings miss -- the company did not have a demand
    # problem until it did. H2-weighted so H1-2024 stays a genuinely clean
    # baseline. Two-sided because every FY2025 driver is unfavourable, and a
    # variance tool that has only ever narrated bad news is less convincing
    # than one that has also explained an underspend.
    #
    #   recruiting (R&D, Jul-Dec 2024, unfavourable): agency fees run hot as the
    #       company hires ahead of the FY2025 headcount ramp already in the
    #       model (R&D 38 -> 52, S&M 40 -> 58). This is the useful kind of
    #       variance -- it is explained by another table in the same dataset.
    #   events (S&M, Sep-Nov 2024, favourable): the user conference is scaled
    #       back, so the spend lands under plan.
    #
    # Defaults of 1.0 reproduce the canonical dataset bit-for-bit. Neither
    # multiplier consumes a random draw, so the noise stream is unchanged.
    actual_fy24_recruiting_mult: float = 1.0    # try 1.90
    actual_fy24_events_mult: float = 1.0        # try 0.62


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
