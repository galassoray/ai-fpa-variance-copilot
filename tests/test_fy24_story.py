"""
test_fy24_story.py
==================
Guards for the FY2024 story multipliers.

Why this input exists
---------------------
FY2024 carried no story: actual-vs-budget revenue variance averaged 0.16% and
every opex line tracked plan, so the first twelve rows of any variance view read
"actual == budget, every month". A clean comparison year is defensible, but it
spends twelve rows of an interviewer's attention proving the engine has nothing
to find.

The story is deliberately constrained, and these tests pin each constraint,
because each one is a claim that has to survive questioning:

  opex-only   -- FY2024 revenue still tracks plan. The company did not have a
                 demand problem until FY2025; saying so is what makes the
                 FY2025 bookings miss land.
  H2-weighted -- H1-2024 stays a genuinely clean baseline.
  two-sided   -- one favourable driver (events underspend) among the
                 unfavourable ones. Every FY2025 driver is unfavourable, and a
                 variance tool that has only ever narrated bad news is less
                 convincing than one that has also explained an underspend.
  secondary   -- FY2025 remains the headline by a wide margin.

Default is 1.0/1.0, which reproduces the canonical dataset bit-for-bit.
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

STORY = dict(actual_fy24_recruiting_mult=1.90, actual_fy24_events_mult=0.62)


def _detail(**changes):
    sc = SCEN.Scenario().with_changes(**changes) if changes else SCEN.Scenario()
    return rp.compute(gen.build_dataset(sc))["variance_detail"]


def _fy24(df):
    return df[df["month"] < "2025-01-01"]


def _fy25(df):
    return df[df["month"] >= "2025-01-01"]


# ---------------------------------------------------------------------------
# the default must change nothing
# ---------------------------------------------------------------------------
def test_fy24_story_is_on_in_the_baseline():
    """The FY2024 story is now part of the canonical dataset (authorized)."""
    sc = SCEN.Scenario()
    assert sc.actual_fy24_recruiting_mult == pytest.approx(1.90)
    assert sc.actual_fy24_events_mult == pytest.approx(0.62)


def test_explicit_multipliers_reproduce_the_default():
    """Passing the baseline multipliers explicitly must equal the default."""
    a = gen.build_dataset(SCEN.Scenario())
    b = gen.build_dataset(SCEN.Scenario().with_changes(**STORY))
    for name in ("fact_actuals", "fact_budget", "fact_forecast"):
        left = a[name].select_dtypes("number").to_numpy(dtype=float)
        right = b[name].select_dtypes("number").to_numpy(dtype=float)
        assert np.array_equal(left, right), f"{name} drifted at explicit multipliers"


def test_neutral_multipliers_recover_the_storyless_year():
    """Turning the story OFF must still work -- the knob is reversible."""
    off = _detail(actual_fy24_recruiting_mult=1.0, actual_fy24_events_mult=1.0)
    d = _fy24(off)
    rec = d[d["account_id"] == "RND_RECRUIT"]
    assert (rec["var_ab_amount"].abs() < 1_000).all()


# ---------------------------------------------------------------------------
# turning it on must not break anything
# ---------------------------------------------------------------------------
def test_all_checks_still_pass_with_the_fy24_story():
    for rec, ev in [(1.60, 0.70), (1.90, 0.62), (2.20, 0.55)]:
        sc = SCEN.Scenario().with_changes(
            actual_fy24_recruiting_mult=rec, actual_fy24_events_mult=ev)
        tables = gen.build_dataset(sc)
        failed = list(vd.run_checks(tables, rp.compute(tables))
                      .query("status == 'FAIL'")["check"])
        assert not failed, f"{rec}/{ev} failed: {failed}"


def test_fy24_story_is_deterministic():
    a = _detail(**STORY)["actual"].to_numpy()
    b = _detail(**STORY)["actual"].to_numpy()
    assert np.array_equal(a, b)


# ---------------------------------------------------------------------------
# the constraints that make the story defensible
# ---------------------------------------------------------------------------
def test_fy24_story_does_not_touch_revenue():
    """FY2024 revenue must still track plan -- the story is on the cost side."""
    for changes in ({}, STORY):
        d = _fy24(_detail(**changes))
        rev = d[d["account_id"].isin(["REV_SUB", "REV_SVC"])]
        worst = (rev["var_ab_amount"].abs() / rev["budget"]).max() * 100
        assert worst < 1.0, "FY2024 revenue should stay close to plan"


def test_fy24_story_lands_in_the_second_half():
    """H1-2024 stays a clean baseline; the story is H2."""
    d = _fy24(_detail(**STORY))
    by_month = d.groupby("month")["oi_impact_ab"].sum()
    h1 = by_month[by_month.index < "2024-07-01"].abs().max()
    h2 = by_month[by_month.index >= "2024-07-01"].abs().max()
    assert h1 < 20_000, "H1-2024 should carry no story"
    assert h2 > 2 * h1, "the story should be visible in H2-2024"


def test_fy24_story_produces_a_favourable_driver():
    """Two-sided by design: the events underspend must show as favourable."""
    d = _fy24(_detail(**STORY))
    ev = d[(d["account_id"] == "SM_EVENTS") & (d["month"] == "2024-09-01")]
    assert not ev.empty
    assert bool(ev["favorable_ab"].iloc[0]), "events underspend should be favourable"
    assert ev["oi_impact_ab"].iloc[0] > 0


def test_fy24_recruiting_is_the_largest_fy24_driver():
    """The story should be the thing the engine surfaces, not buried in noise."""
    d = _fy24(_detail(**STORY))
    dec = d[d["month"] == "2024-12-01"]
    top = dec.reindex(dec["oi_impact_ab"].abs().sort_values(ascending=False).index)
    assert top["account_id"].iloc[0] == "RND_RECRUIT"


def test_fy25_remains_the_headline():
    """FY2024 must not compete with the story the portfolio narrative tells."""
    d = _detail(**STORY)
    worst24 = _fy24(d)["oi_impact_ab"].abs().max()
    worst25 = _fy25(d)["oi_impact_ab"].abs().max()
    assert worst25 > 3 * worst24, (
        f"FY24 worst {worst24:,.0f} is too close to FY25 worst {worst25:,.0f}")


def test_fy24_story_moves_fy24_but_leaves_fy25_alone():
    """The knob must be year-scoped, not a global spend change."""
    off = _detail(actual_fy24_recruiting_mult=1.0, actual_fy24_events_mult=1.0)
    on = _detail()
    assert np.array_equal(_fy25(off)["actual"].to_numpy(),
                          _fy25(on)["actual"].to_numpy()), \
        "FY2025 actuals must be untouched by the FY2024 knobs"
    assert not np.array_equal(_fy24(off)["actual"].to_numpy(),
                              _fy24(on)["actual"].to_numpy())
