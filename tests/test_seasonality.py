"""
test_seasonality.py
===================
Guards for the bookings-seasonality inputs.

Why this input exists
---------------------
Recognized revenue is derived as ending ARR / 12, and ARR is a STOCK. Noise on
the FLOWS (~3% of ~$500K of new ARR) is diluted roughly 60:1 against a $26M ARR
base, so it moves revenue by ~0.06%. The baseline revenue series is therefore
very nearly a straight line (trailing-12 R^2 = 0.9996).

Seasonality is applied to the FLOWS, and to budget/actual/forecast alike. That
matters twice over:

  * revenue stays exactly ARR / 12, so the ARR bridge still closes and the
    volume/price decomposition still ties to the P&L; and
  * because the plan is seasonal too, seasonality does not masquerade as
    variance -- the engineered FY2025 story is what still drives the miss.

The default is 0.0. These tests pin BOTH halves: that the default changes
nothing, and that turning the knob up breaks no reconciliation.
"""
from __future__ import annotations
import os
import sys

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import scenario as SCEN                      # noqa: E402
import generate_synthetic_data as gen        # noqa: E402
import run_pipeline as rp                    # noqa: E402
from validation import validate_data as vd   # noqa: E402

SEASONAL = dict(bookings_seasonality=0.25, churn_seasonality=0.30)


def _revenue_series(tables, which="fact_actuals"):
    d = tables[which]
    return (d[d["account_id"].isin(["REV_SUB", "REV_SVC"])]
            .groupby("month")["amount"].sum())


def _r2_of_linear_fit(y):
    x = np.arange(len(y))
    slope, icept = np.polyfit(x, y, 1)
    pred = slope * x + icept
    return 1 - ((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum()


# ---------------------------------------------------------------------------
# the default must change nothing
# ---------------------------------------------------------------------------
def test_seasonality_is_on_in_the_baseline():
    """Seasonality is now part of the canonical dataset (authorized change).

    Was test_seasonality_defaults_to_off. Inverted rather than deleted: the
    default is a decision, so it stays pinned in whichever direction it points.
    """
    sc = SCEN.Scenario()
    assert sc.bookings_seasonality == pytest.approx(0.25)
    assert sc.churn_seasonality == pytest.approx(0.30)


def test_explicit_amplitudes_reproduce_the_default():
    """Passing the baseline amplitudes explicitly must equal the default.

    This is the reversibility guard: it proves the knob still fully controls the
    behaviour, so setting it back to 0.0 recovers the pre-seasonality company.
    """
    a = gen.build_dataset(SCEN.Scenario())
    b = gen.build_dataset(SCEN.Scenario().with_changes(
        bookings_seasonality=0.25, churn_seasonality=0.30))
    for name in ("fact_actuals", "fact_budget", "fact_forecast",
                 "fact_saas_metrics"):
        left = a[name].select_dtypes("number").to_numpy(dtype=float)
        right = b[name].select_dtypes("number").to_numpy(dtype=float)
        assert np.array_equal(left, right), f"{name} drifted at explicit amplitude"


def test_zero_amplitude_recovers_the_flat_series():
    """Turning seasonality OFF must still work -- the knob is reversible."""
    flat = gen.build_dataset(SCEN.Scenario().with_changes(
        bookings_seasonality=0.0, churn_seasonality=0.0))
    y = _revenue_series(flat).to_numpy()[-12:]
    assert _r2_of_linear_fit(y) > 0.999


# ---------------------------------------------------------------------------
# turning it on must not break anything
# ---------------------------------------------------------------------------
def test_all_checks_still_pass_under_seasonality():
    for amp, churn in [(0.15, 0.20), (0.25, 0.30), (0.35, 0.30)]:
        sc = SCEN.Scenario().with_changes(
            bookings_seasonality=amp, churn_seasonality=churn)
        tables = gen.build_dataset(sc)
        df = vd.run_checks(tables, rp.compute(tables))
        failed = list(df[df["status"] == "FAIL"]["check"])
        assert not failed, f"amplitude {amp}/{churn} failed: {failed}"


def test_revenue_still_equals_arr_over_twelve_under_seasonality():
    """The identity the volume/price decomposition depends on."""
    sc = SCEN.Scenario().with_changes(**SEASONAL)
    tables = gen.build_dataset(sc)
    sub = tables["fact_actuals"].query("account_id == 'REV_SUB'")
    sub = sub.set_index("month")["amount"]
    arr = tables["fact_saas_metrics"].set_index("month")["ending_arr"] / 12.0
    assert (sub - arr).abs().max() <= 0.01


def test_seasonality_does_not_manufacture_budget_variance():
    """Seasonality is in the PLAN too, so it must not show up as a miss.

    If it only hit actuals it would swamp the engineered FY2025 story, and the
    variance commentary would be narrating a calendar artifact.
    """
    base = gen.build_dataset(SCEN.Scenario())
    seas = gen.build_dataset(SCEN.Scenario().with_changes(**SEASONAL))
    for tables in (base, seas):
        a = _revenue_series(tables, "fact_actuals")
        b = _revenue_series(tables, "fact_budget")
        fy24 = ((a - b) / b * 100).iloc[:12].abs().mean()
        assert fy24 < 1.0, "FY2024 should stay a clean comparison year"
    # and the FY2025 story survives at roughly its original magnitude
    def fy25_miss(tables):
        a = _revenue_series(tables, "fact_actuals")
        b = _revenue_series(tables, "fact_budget")
        return ((a - b) / b * 100).iloc[-1]
    assert fy25_miss(seas) == pytest.approx(fy25_miss(base), abs=0.5)


def test_seasonality_actually_bends_the_revenue_line():
    """The knob must do the thing it exists to do."""
    base = _revenue_series(gen.build_dataset(SCEN.Scenario().with_changes(
        bookings_seasonality=0.0, churn_seasonality=0.0))).to_numpy()[-12:]
    seas = _revenue_series(gen.build_dataset(SCEN.Scenario())).to_numpy()[-12:]
    assert _r2_of_linear_fit(base) > 0.999          # the problem
    assert _r2_of_linear_fit(seas) < 0.99           # the fix
    # month-over-month growth should visibly widen
    spread = lambda y: pd.Series(y).pct_change().dropna().max() - \
                       pd.Series(y).pct_change().dropna().min()
    assert spread(seas) > spread(base) * 2


def test_seasonality_is_deterministic():
    sc = SCEN.Scenario().with_changes(**SEASONAL)
    a = rp.compute(gen.build_dataset(sc))["variance_detail"]["actual"].to_numpy()
    b = rp.compute(gen.build_dataset(sc))["variance_detail"]["actual"].to_numpy()
    assert np.array_equal(a, b)


def test_ending_arr_is_broadly_unchanged():
    """Seasonality redistributes bookings within the year; it should not
    quietly re-scale the company the portfolio narrative describes."""
    base = gen.build_dataset(SCEN.Scenario())["fact_saas_metrics"]
    seas = gen.build_dataset(SCEN.Scenario().with_changes(
        **SEASONAL))["fact_saas_metrics"]
    b_end = base["ending_arr"].iloc[-1]
    s_end = seas["ending_arr"].iloc[-1]
    assert abs(s_end - b_end) / b_end < 0.01
