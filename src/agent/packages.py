"""
packages.py
===========
Goal context and the canonical hand-written variance-package plan.

THE DETERMINISTIC BASELINE
--------------------------
This module is the *control condition*. It produces the complete monthly
variance package with no model anywhere in the loop, which serves three purposes
beyond being a fallback:

  1. It is the eval baseline. Phase 3's planner is scored against this plan
     (step recall, step precision, dependency-order validity), which is only
     meaningful if a reference plan exists.
  2. It is the numeric ground truth. Whatever the planner produces, the figures
     must match what this produces, because both route through the same tools.
  3. It is the honest comparison. Side by side with the agent it answers the
     question a senior interviewer will actually ask -- *"is the agent earning
     its keep here?"* -- with measured tokens, latency, and cost rather than an
     opinion. For a fixed monthly close the pipeline is very likely the right
     answer; the agent earns its place when the goal space is open. Knowing
     which is which, with numbers behind it, is the point.

GOAL FIELDS ARE COMPUTED, NEVER MODELED
----------------------------------------
``build_goal`` derives fiscal-year start, prior period, and the trend window
from the dimension tables in code. Date arithmetic is arithmetic, and the model
has no arithmetic capability -- so a plan referring to ``$GOAL.fy_start`` is
referring to a figure the deterministic layer computed, exactly like every other
number in the system.
"""
from __future__ import annotations

from agent.plan import Plan, Step

#: Section names the package is expected to contain. A plan that promises one
#: and does not produce it is flagged by the coverage check.
SECTION_HEADLINE = "operating_headline"
SECTION_PL = "pl_summary"
SECTION_DRIVERS = "department_drivers"
SECTION_TOP_DECOMP = "top_driver_decomposition"
SECTION_SECOND_DECOMP = "second_driver_decomposition"
SECTION_THIRD_DECOMP = "third_driver_decomposition"
SECTION_FOURTH_DECOMP = "fourth_driver_decomposition"
SECTION_FIFTH_DECOMP = "fifth_driver_decomposition"
SECTION_COMP = "compensation_decomposition"
SECTION_REVENUE = "revenue_decomposition"
SECTION_ARR = "arr_bridge"
SECTION_HEADCOUNT = "headcount"
SECTION_TREND = "opex_trend"

REQUIRED_SECTIONS = [
    SECTION_HEADLINE, SECTION_PL, SECTION_DRIVERS,
    SECTION_TOP_DECOMP, SECTION_SECOND_DECOMP, SECTION_THIRD_DECOMP,
    SECTION_FOURTH_DECOMP, SECTION_FIFTH_DECOMP,
    SECTION_COMP, SECTION_REVENUE, SECTION_ARR, SECTION_HEADCOUNT,
]

#: Every department is decomposed, not just the top two.
#:
#: The earlier plan stopped at two on the reasoning that a monthly package only
#: needs the largest drivers. That was optimising leanness that costs about ten
#: milliseconds -- three additional local queries -- and it left the package
#: genuinely incomplete: a close pack that explains two of five departments is
#: not the whole picture, and it cannot produce a variance packet for the three
#: owners it skipped.
DECOMP_SECTIONS = [SECTION_TOP_DECOMP, SECTION_SECOND_DECOMP,
                   SECTION_THIRD_DECOMP, SECTION_FOURTH_DECOMP,
                   SECTION_FIFTH_DECOMP]


class GoalError(ValueError):
    """The goal cannot be served. Carries a refusal reason code."""

    def __init__(self, reason_code: str, detail: str = ""):
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)
        self.reason_code = reason_code
        self.detail = detail


def build_goal(con, period: str, comparison: str = "actual_vs_budget") -> dict:
    """Deterministic goal context, derived from the dimension tables.

    Raises GoalError with a reason code when the period cannot be served, so
    that refusal is a structured outcome the eval can measure rather than free
    text. Refusal recall and false-refusal rate are both eval metrics; neither
    is measurable over prose.
    """
    from agent.registry import PeriodParam, ValidationContext

    ctx = ValidationContext(con)
    try:
        canonical, _ = PeriodParam("goal period").validate(period, ctx)
    except ValueError as e:
        raise GoalError("PERIOD_NOT_IN_DATASET", str(e)) from None

    row = con.execute(
        "SELECT fiscal_year, month_number, quarter FROM dim_date "
        "WHERE CAST(month AS VARCHAR) = ?", [canonical]
    ).fetchone()
    if row is None:
        raise GoalError("PERIOD_NOT_IN_DATASET", canonical)
    fiscal_year, month_number, quarter = row

    n = con.execute(
        "SELECT COUNT(*) FROM out_variance_detail WHERE month = ?", [canonical]
    ).fetchone()[0]
    if n == 0:
        raise GoalError("NO_DATA_FOR_PERIOD", canonical)

    if comparison == "actual_vs_forecast":
        n_fc = con.execute(
            "SELECT COUNT(*) FROM out_variance_detail "
            "WHERE month = ? AND forecast IS NOT NULL", [canonical]
        ).fetchone()[0]
        if n_fc == 0:
            raise GoalError("COMPARISON_UNAVAILABLE",
                            f"no forecast exists for {canonical}")

    fy_start = con.execute(
        "SELECT MIN(CAST(month AS VARCHAR)) FROM dim_date WHERE fiscal_year = ? "
        "AND CAST(month AS VARCHAR) <= ?", [fiscal_year, canonical]
    ).fetchone()[0]

    prior = con.execute(
        "SELECT MAX(CAST(month AS VARCHAR)) FROM dim_date "
        "WHERE CAST(month AS VARCHAR) < ?", [canonical]
    ).fetchone()[0]

    return {
        "period": canonical,
        "comparison": comparison,
        "fiscal_year": int(fiscal_year),
        "month_number": int(month_number),
        "quarter": str(quarter),
        "fy_start": fy_start,
        "prior_period": prior,
    }


def variance_package_plan(goal: dict) -> Plan:
    """The canonical monthly variance package.

    Eleven steps. Two of them -- the decompositions -- depend on data that does
    not exist at plan time and are bound by symbolic reference to the ranking
    step's output. That is the mechanism that makes a fixed plan skeleton
    adaptive, and it is why the plan can be written once and run for any period.

    Only the trend step is optional. Everything else is a promised section: if
    it cannot be produced, the run refuses rather than quietly shipping a
    package with a hole in it.
    """
    cmp_ = goal.get("comparison", "actual_vs_budget")
    return Plan(
        goal=f"prepare the {goal['period']} variance package ({cmp_})",
        promises=list(REQUIRED_SECTIONS),
        steps=[
            Step(1, "list_periods", {},
                 purpose="period_coverage",
                 optional=True),

            Step(2, "get_operating_metrics", {"period": "$GOAL.period"},
                 purpose=SECTION_HEADLINE),

            Step(3, "get_pl_summary",
                 {"period": "$GOAL.period", "comparison": "$GOAL.comparison"},
                 purpose=SECTION_PL),

            # top_n must cover every department, because the decomposition
            # steps below bind to rows[0..4] of this result.
            Step(4, "rank_variance_drivers",
                 {"period": "$GOAL.period", "dimension": "department",
                  "comparison": "$GOAL.comparison", "top_n": 5},
                 purpose=SECTION_DRIVERS),

            # Bound at execution time from step 4, one per department in rank
            # order. The model never reads step 4's output and retypes an id.
            *[Step(5 + i, "decompose_variance",
                   {"period": "$GOAL.period",
                    "department_id": f"$STEP_4.rows[{i}].member",
                    "comparison": "$GOAL.comparison", "top_n": 5},
                   purpose=DECOMP_SECTIONS[i])
              for i in range(len(DECOMP_SECTIONS))],

            Step(10, "get_comp_decomposition", {"period": "$GOAL.period"},
                 purpose=SECTION_COMP),

            Step(11, "get_revenue_decomposition", {"period": "$GOAL.period"},
                 purpose=SECTION_REVENUE),

            Step(12, "get_arr_bridge", {"period": "$GOAL.period"},
                 purpose=SECTION_ARR),

            Step(13, "get_headcount_movement", {"period": "$GOAL.period"},
                 purpose=SECTION_HEADCOUNT),

            # Enrichment: seasonality context. Its absence weakens the package
            # but does not invalidate it, so it does not block the run.
            Step(14, "get_trend",
                 {"metric": "opex", "start_period": "$GOAL.fy_start",
                  "end_period": "$GOAL.period"},
                 purpose=SECTION_TREND, optional=True),
        ],
    )


def package_digest(result) -> dict:
    """A compact, stable view of a completed package.

    Used as the golden-file target: it pins the shape *and* the headline figures,
    so a change to either the plan or the computation layer shows up as a diff
    rather than as a silently different package.
    """
    out = {
        "outcome": result.outcome,
        "complete": result.complete,
        "missing_sections": list(result.missing_sections),
        "sections": {},
    }
    for name in sorted(result.sections):
        sec = result.sections[name]
        out["sections"][name] = {
            "tool": sec["tool"],
            "rows": len(sec["rows"]),
            "hash": sec["result_hash"],
        }
    return out
