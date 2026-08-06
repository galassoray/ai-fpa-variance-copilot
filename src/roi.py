"""
roi.py -- the quantified ROI claim, built the same way as everything else.

WHY THIS FILE EXISTS
--------------------
INSTRUCTIONS.md sec.3 requires every tool to prove three things: numeric
integrity, guardrails, and quantified ROI. The first two are airtight and
demonstrable. The third did not exist anywhere in this repo.

The obvious fix -- writing "saves ~20 hours a month" in the README -- would have
been the worst possible move for THIS tool specifically. The entire thesis here
is that no decision-facing number is asserted without being computed and
checked. An unsourced ROI headline would be the one fabricated figure in a
project whose headline is "zero fabricated figures," and an interviewer who
noticed would be right to discount everything else.

So the ROI claim is built under the same discipline as the commentary:

    MEASURED   -- instrumented from the actual pipeline at run time. Not typed
                  in by hand, not remembered, not rounded up.
    ASSUMED    -- the manual baseline. This CANNOT be measured here: doing so
                  would require timing real analysts on real close cycles at a
                  real employer, which this project deliberately has no access
                  to (sec.3 rule 1, synthetic/public data only). So every
                  assumption is named, defaulted conservatively, carries a
                  stated basis, and is exposed as an input.
    DERIVED    -- computed from the two above, and reported as a RANGE with a
                  sensitivity table, never as a single hero number.

The honest sentence this produces is:

    "Measured: the tool computes and self-verifies 16 figures of variance
     commentary in 14 milliseconds. Assumed: an analyst spends N minutes
     drafting and tying out the same commentary. Under those assumptions the
     saving is X-Y hours per close, and here is the sensitivity."

That sentence survives a hostile question. "Saves 20 hours a month" does not.

OWNERSHIP NOTE (INSTRUCTIONS.md sec.4)
--------------------------------------
Ray owns the problem framing, and the baseline assumptions ARE problem framing.
The defaults below are a starting point drawn from generic FP&A close practice,
NOT from any employer's process. They are placeholders to be replaced with
numbers Ray can defend from his own experience of what a monthly variance
package actually takes. The model is the deliverable; the assumptions are his.

WHAT THIS DELIBERATELY DOES NOT CLAIM
-------------------------------------
  * It does not claim to save the ANALYSIS. Deciding whether a $67K salary
    variance is a hiring-timing problem or a forecasting problem is judgment,
    and the tool does not do it.
  * It does not claim to save the CLOSE. Getting to a trial balance is
    upstream of anything here.
  * It does not claim headcount reduction. The saving is redeployed analyst
    hours, which is a different and more honest claim -- and the one a finance
    leader will actually believe.
  * It does not annualize a single good month into a headline. The unit is one
    close cycle, scaled explicitly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict, replace


# =============================================================================
# ASSUMED -- the manual baseline. Every field is an assumption, not a finding.
# =============================================================================
@dataclass(frozen=True)
class Baseline:
    """What producing the same commentary costs without the tool.

    Each default carries its basis in the comment. None of these are measured
    here and none should ever be presented as measured.
    """

    # Drafting the narrative once the numbers exist: writing the paragraph that
    # explains the top drivers, the decompositions and the retention line.
    # Basis: a monthly variance package of this scope (company + 5 drivers +
    # comp decomposition + ARR bridge) is a half-morning task for most analysts.
    # Defaulted to the CONSERVATIVE end of that.
    drafting_minutes: float = 75.0

    # Tying each figure in the narrative back to its source. This is the piece
    # the tool most directly replaces, and the one with the firmest unit: it
    # scales with the number of figures cited, which IS measured.
    # Basis: tracing one figure to a source report and confirming it -- pulling
    # the schedule, filtering to the line, matching the number.
    tie_out_minutes_per_figure: float = 1.5

    # Review cycles: reviewer finds an issue, it goes back, it gets fixed.
    # Basis: most variance packages take at least one round of comments; two is
    # common when figures are hand-transcribed.
    review_cycles: float = 2.0
    minutes_per_review_cycle: float = 20.0

    # Fully-loaded cost of the analyst doing this work.
    # Basis: total employer cost (salary + benefits + overhead) / 2,080 hours.
    # Defaulted to a mid-market Senior FP&A Analyst. REPLACE with a figure
    # appropriate to whoever you are talking to.
    loaded_cost_per_hour: float = 85.0

    # How many times this happens. One close per month by default; scope is the
    # number of separate commentaries produced per close (e.g. one per entity,
    # per region, or per department rollup).
    closes_per_year: int = 12
    commentaries_per_close: int = 1


@dataclass(frozen=True)
class WithTool:
    """What producing the same commentary costs WITH the tool.

    Note what is NOT zero here. An analyst still reads the draft, still applies
    judgment, still edits. A model that claims the human cost goes to zero is
    describing a tool nobody should deploy.
    """

    # Reading the generated draft, applying judgment, editing the wording.
    # This is the irreducible part and it is deliberately not small.
    review_and_edit_minutes: float = 20.0

    # Spot-checking the audit rather than tying out every figure by hand. The
    # audit has already verified each figure against its computed value; a
    # sensible analyst still samples it.
    # Basis: sampling a handful of the green chips against the source tables.
    spot_check_minutes: float = 5.0

    # Review cycles drop because transcription errors -- the usual cause of a
    # round trip -- are structurally eliminated, not because reviewers get
    # less careful.
    review_cycles: float = 1.0
    minutes_per_review_cycle: float = 15.0


# =============================================================================
# MEASURED -- instrumented from the real pipeline. Never hand-entered.
# =============================================================================
@dataclass
class Measured:
    """Facts about the tool, obtained by running it."""
    figures_per_commentary: float = 0.0
    words_per_commentary: float = 0.0
    seconds_per_commentary: float = 0.0
    months_measured: int = 0
    variance_rows_computed: int = 0
    allowed_values_per_fact_pack: int = 0

    @property
    def machine_minutes_per_commentary(self) -> float:
        return self.seconds_per_commentary / 60.0


def measure(outputs: dict, tables: dict) -> Measured:
    """Run the real narrative + audit path and time it.

    Imports are local so that importing this module does not pull in the
    narrative layer for callers who only want the dataclasses.
    """
    from narrative import fact_pack as FP, prompt as P
    from guardrails import numeric_audit as na

    months = sorted(outputs["variance_detail"]["month"].unique())
    figures = words = 0
    packs = []

    start = time.perf_counter()
    for m in months:
        pack = FP.build_fact_pack(m, outputs, tables)
        if pack.status != "ok":
            continue
        text = P.injection_narrative(pack)
        result = na.audit(text, pack)
        figures += len(result.matched)
        words += len(text.split())
        packs.append(pack)
    elapsed = time.perf_counter() - start

    n = max(len(packs), 1)
    return Measured(
        figures_per_commentary=figures / n,
        words_per_commentary=words / n,
        seconds_per_commentary=elapsed / n,
        months_measured=len(packs),
        variance_rows_computed=len(outputs["variance_detail"]),
        allowed_values_per_fact_pack=len(packs[-1].allowed_values) if packs else 0,
    )


# =============================================================================
# DERIVED -- computed from measured + assumed. Reported as a range.
# =============================================================================
@dataclass
class RoiResult:
    baseline_minutes: float
    tool_minutes: float
    saved_minutes: float
    baseline_breakdown: dict
    tool_breakdown: dict

    @property
    def saved_hours_per_commentary(self) -> float:
        return self.saved_minutes / 60.0

    @property
    def pct_reduction(self) -> float:
        return 0.0 if not self.baseline_minutes else \
            self.saved_minutes / self.baseline_minutes

    def annual_hours(self, base: Baseline) -> float:
        return (self.saved_minutes * base.closes_per_year
                * base.commentaries_per_close) / 60.0

    def annual_dollars(self, base: Baseline) -> float:
        return self.annual_hours(base) * base.loaded_cost_per_hour


def compute_roi(measured: Measured,
                base: Baseline | None = None,
                tool: WithTool | None = None) -> RoiResult:
    """The arithmetic. Note the tie-out term is measured x assumed, not guessed.

    tie_out = (figures actually cited, MEASURED)
              x (minutes to tie out one figure, ASSUMED)

    That product is the core of the claim, and it is the honest shape: the part
    that scales is measured, the per-unit rate is an assumption on the table.
    """
    base = base or Baseline()
    tool = tool or WithTool()

    b_tie = measured.figures_per_commentary * base.tie_out_minutes_per_figure
    b_review = base.review_cycles * base.minutes_per_review_cycle
    b_breakdown = {
        "drafting": base.drafting_minutes,
        "tie-out": b_tie,
        "review cycles": b_review,
    }
    baseline_minutes = sum(b_breakdown.values())

    t_review = tool.review_cycles * tool.minutes_per_review_cycle
    t_breakdown = {
        "machine time": measured.machine_minutes_per_commentary,
        "review and edit": tool.review_and_edit_minutes,
        "spot-check the audit": tool.spot_check_minutes,
        "review cycles": t_review,
    }
    tool_minutes = sum(t_breakdown.values())

    return RoiResult(
        baseline_minutes=baseline_minutes,
        tool_minutes=tool_minutes,
        saved_minutes=baseline_minutes - tool_minutes,
        baseline_breakdown=b_breakdown,
        tool_breakdown=t_breakdown,
    )


# -----------------------------------------------------------------------------
# Sensitivity: the point estimate is never the deliverable.
# -----------------------------------------------------------------------------
SCENARIOS = {
    # A skeptical reader's version: fast analyst, light process, cheap hour.
    "conservative": (
        Baseline(drafting_minutes=40, tie_out_minutes_per_figure=0.75,
                 review_cycles=1, minutes_per_review_cycle=15,
                 loaded_cost_per_hour=70),
        WithTool(review_and_edit_minutes=25, spot_check_minutes=8,
                 review_cycles=1, minutes_per_review_cycle=15),
    ),
    "base": (Baseline(), WithTool()),
    # A heavier shop: more entities, more scrutiny, more round trips.
    "heavier process": (
        Baseline(drafting_minutes=110, tie_out_minutes_per_figure=2.5,
                 review_cycles=3, minutes_per_review_cycle=25,
                 loaded_cost_per_hour=100),
        WithTool(review_and_edit_minutes=20, spot_check_minutes=5,
                 review_cycles=1, minutes_per_review_cycle=20),
    ),
}


def sensitivity(measured: Measured) -> list[dict]:
    rows = []
    for name, (base, tool) in SCENARIOS.items():
        r = compute_roi(measured, base, tool)
        rows.append({
            "scenario": name,
            "baseline_min": round(r.baseline_minutes, 1),
            "tool_min": round(r.tool_minutes, 1),
            "saved_min": round(r.saved_minutes, 1),
            "pct_reduction": round(r.pct_reduction * 100, 1),
            "annual_hours": round(r.annual_hours(base), 1),
            "annual_dollars": round(r.annual_dollars(base), 0),
        })
    return rows


def headline(measured: Measured) -> str:
    """The claim, phrased so the assumption travels with the number.

    Deliberately never returns a bare figure. If someone quotes this line, the
    conditional comes along with it.
    """
    rows = sensitivity(measured)
    lo = min(r["saved_min"] for r in rows) / 60.0
    hi = max(r["saved_min"] for r in rows) / 60.0
    return (
        f"Measured: {measured.figures_per_commentary:.0f} figures computed and "
        f"self-verified per commentary in "
        f"{measured.seconds_per_commentary * 1000:.0f} ms, across "
        f"{measured.months_measured} months. "
        f"Assumed: an analyst drafts and ties out the same commentary by hand. "
        f"Under the stated assumptions that is {lo:.1f}-{hi:.1f} hours saved per "
        f"commentary, {rows[1]['pct_reduction']:.0f}% in the base case. "
        f"The range is the point: the tool's side is measured, the manual "
        f"baseline is not, and the saving moves with assumptions the reader "
        f"can change."
    )


def report(outputs: dict, tables: dict) -> str:
    m = measure(outputs, tables)
    lines = [
        "=" * 74,
        "QUANTIFIED ROI -- measured where measurable, assumed where not",
        "=" * 74,
        "",
        "MEASURED (instrumented from this run)",
        f"  figures verified per commentary : {m.figures_per_commentary:.1f}",
        f"  words per commentary            : {m.words_per_commentary:.0f}",
        f"  machine time per commentary     : {m.seconds_per_commentary*1000:.0f} ms",
        f"  months measured                 : {m.months_measured}",
        f"  variance rows computed          : {m.variance_rows_computed:,}",
        f"  computed values per fact pack   : {m.allowed_values_per_fact_pack}",
        "",
        "ASSUMED (the manual baseline -- not measured, stated so)",
    ]
    b = Baseline()
    for k, v in asdict(b).items():
        lines.append(f"  {k:32}: {v}")
    lines += ["", "DERIVED (sensitivity -- the point estimate is not the deliverable)", ""]
    hdr = f"  {'scenario':<16}{'baseline':>10}{'with tool':>11}{'saved':>9}{'reduction':>11}{'hrs/yr':>9}"
    lines += [hdr, "  " + "-" * (len(hdr) - 2)]
    for r in sensitivity(m):
        lines.append(
            f"  {r['scenario']:<16}{r['baseline_min']:>8.0f}m{r['tool_min']:>10.0f}m"
            f"{r['saved_min']:>8.0f}m{r['pct_reduction']:>10.0f}%{r['annual_hours']:>9.0f}")
    lines += ["", "HEADLINE", "  " + headline(m), "",
              "NOT CLAIMED", "  the analysis, the close, or headcount reduction.",
              "  the saving is redeployed analyst hours on one narrow task.", "=" * 74]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import generate_synthetic_data as gen
    import run_pipeline as rp
    from scenario import Scenario

    tables = gen.build_dataset(Scenario())
    print(report(rp.compute(tables), tables))
