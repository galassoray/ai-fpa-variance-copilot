"""
test_roi.py
===========
Guards for the ROI claim.

Most of these are not arithmetic tests. The arithmetic is trivial; what needs
protecting is the HONESTY of the claim, because that is what an interviewer
actually probes and what would quietly rot over time. Specifically:

  * the measured half must really be measured (instrumented, not hand-entered);
  * the assumed half must stay visible and adjustable;
  * the headline must never hand out a bare number without its condition;
  * the tool's own cost must never be modelled as zero;
  * a conservative reading must produce a modest result, because a model that
    shows a huge win under every assumption is not a model, it is a poster.
"""
from __future__ import annotations
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import roi as ROI                             # noqa: E402
import scenario as SCEN                       # noqa: E402
import generate_synthetic_data as gen         # noqa: E402
import run_pipeline as rp                     # noqa: E402


@pytest.fixture(scope="module")
def measured():
    tables = gen.build_dataset(SCEN.Scenario())
    return ROI.measure(rp.compute(tables), tables)


# ---------------------------------------------------------------------------
# the measured half must actually be measured
# ---------------------------------------------------------------------------
def test_measurement_comes_from_running_the_pipeline(measured):
    assert measured.months_measured == 24
    assert measured.variance_rows_computed == 624
    # 16 figures per commentary is the current shape of the deterministic
    # narrative (company line + top drivers + comp decomposition + ARR bridge).
    # If this drifts the ROI model is describing a different artifact.
    assert 10 <= measured.figures_per_commentary <= 25
    assert measured.seconds_per_commentary > 0, "timing must be real, not zero"
    assert measured.seconds_per_commentary < 5, "a commentary should not take seconds"


def test_tie_out_term_scales_with_the_measured_figure_count(measured):
    """The core of the claim: measured count x assumed rate.

    If the figure count doubled, the tie-out saving must double. This is what
    makes the claim scale honestly rather than being a flat guess.
    """
    doubled = ROI.Measured(**{**measured.__dict__,
                              "figures_per_commentary":
                                  measured.figures_per_commentary * 2})
    base = ROI.Baseline()
    r1 = ROI.compute_roi(measured, base)
    r2 = ROI.compute_roi(doubled, base)
    delta = r2.baseline_breakdown["tie-out"] - r1.baseline_breakdown["tie-out"]
    assert delta == pytest.approx(
        measured.figures_per_commentary * base.tie_out_minutes_per_figure)


# ---------------------------------------------------------------------------
# the assumed half must stay visible and movable
# ---------------------------------------------------------------------------
def test_every_baseline_assumption_moves_the_answer(measured):
    """No assumption may be decorative. If changing it does nothing, it is
    misleading to display it as an input."""
    base = ROI.Baseline()
    reference = ROI.compute_roi(measured, base).saved_minutes
    for field_name in ("drafting_minutes", "tie_out_minutes_per_figure",
                       "review_cycles", "minutes_per_review_cycle"):
        bumped = ROI.Baseline(**{**base.__dict__,
                                 field_name: getattr(base, field_name) * 2 + 1})
        assert ROI.compute_roi(measured, bumped).saved_minutes != reference, \
            f"{field_name} does not affect the result"


def test_the_tool_is_never_modelled_as_free(measured):
    """An analyst still reads, edits and reviews. A tool that claims the human
    cost goes to zero is describing something nobody should deploy."""
    r = ROI.compute_roi(measured)
    assert r.tool_minutes > 20, "with-tool cost is implausibly low"
    assert r.pct_reduction < 0.95, "a >95% reduction claim will not survive scrutiny"


# ---------------------------------------------------------------------------
# the shape of the claim
# ---------------------------------------------------------------------------
def test_sensitivity_spans_a_real_range(measured):
    rows = ROI.sensitivity(measured)
    assert len(rows) >= 3
    saved = [r["saved_min"] for r in rows]
    assert min(saved) < max(saved) / 2, \
        "scenarios must actually diverge, or the sensitivity table is theatre"


def test_a_conservative_reading_is_modest(measured):
    """The credibility test. Under a skeptic's assumptions -- fast analyst,
    light process -- the saving must come out small. A model that wins big
    under every assumption is not a model."""
    base, tool = ROI.SCENARIOS["conservative"]
    r = ROI.compute_roi(measured, base, tool)
    assert r.saved_minutes > 0, "even conservatively the tool should help"
    assert r.pct_reduction < 0.50, \
        "the conservative case is not conservative enough to be believed"


def test_headline_never_states_a_bare_number(measured):
    """If someone quotes the headline, the condition must travel with it."""
    h = ROI.headline(measured)
    assert "Measured:" in h and "Assumed:" in h
    assert "assumptions" in h.lower()
    # a range, not a point
    assert "-" in h.split("hours saved")[0]


def test_report_names_what_is_not_claimed(measured):
    tables = gen.build_dataset(SCEN.Scenario())
    text = ROI.report(rp.compute(tables), tables)
    low = text.lower()
    assert "not claimed" in low
    assert "headcount" in low, "must explicitly disclaim headcount reduction"
    for section in ("MEASURED", "ASSUMED", "DERIVED"):
        assert section in text, f"{section} section missing from the report"


def test_annualisation_is_explicit_not_baked_in(measured):
    """Annual figures must follow from a stated cadence, so a reader can see
    that a monthly saving was multiplied by twelve rather than asserted."""
    r = ROI.compute_roi(measured)
    monthly = ROI.Baseline(closes_per_year=12, commentaries_per_close=1)
    quarterly = ROI.Baseline(closes_per_year=4, commentaries_per_close=1)
    assert r.annual_hours(monthly) == pytest.approx(r.annual_hours(quarterly) * 3)
    multi = ROI.Baseline(closes_per_year=12, commentaries_per_close=5)
    assert r.annual_hours(multi) == pytest.approx(r.annual_hours(monthly) * 5)
