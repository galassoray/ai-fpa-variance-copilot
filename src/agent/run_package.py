"""
run_package.py
==============
CLI for the deterministic variance package. No model, no API key, no network.

    python src/agent/run_package.py 2025-09
    python src/agent/run_package.py 2025-09 --json > run.json
    python src/agent/run_package.py 2029-01          # demonstrates refusal

Two reasons this exists beyond convenience:

* It is the **control condition** for the Phase 3 comparison. Run it, note the
  wall clock and the zero token cost, then run the agent on the same goal. The
  difference is the agent's price, measured rather than estimated.
* A saved ``--json`` run is a **replayable artifact**. Phase 5's demo path
  replays these with no key attached, so an interviewer sees complete, correct,
  deterministic behavior in two seconds rather than watching a spinner.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, ".."))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from agent import materialize as mz            # noqa: E402
from agent import registry as reg              # noqa: E402
from agent import tools as _tools              # noqa: E402,F401  (registers tools)
from agent.orchestrator import Orchestrator    # noqa: E402
from agent.packages import GoalError, build_goal, variance_package_plan  # noqa: E402


def _money(v):
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        neg = v < 0
        s = f"${abs(v):,.0f}"
        return f"({s})" if neg else s       # accounting convention, as in the copilot
    return str(v)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
# Sections are rendered by the TOOL that produced them, not by the label the
# plan gave them.
#
# The first implementation keyed off section names -- "operating_headline",
# "arr_bridge" -- which worked for the hand-written plan and almost nothing
# else. A live agent plan produced "top_department_account_decomp" and
# "operating_metrics", so the renderer silently dropped four of five sections
# and printed only the P&L. The package looked empty even though every step
# had succeeded.
#
# A planner must be free to name its own sections; presentation is a display
# concern and should not constrain what the planner may produce. Keying on the
# tool inverts that dependency: any plan renders, ordering follows the plan,
# and a tool added later still displays -- via its own formatter if one is
# registered, or the generic table if not.

def _fmt_row(row: dict, cols: list, widths: list) -> str:
    out = []
    for c, w in zip(cols, widths):
        v = row.get(c)
        v = _money(v) if isinstance(v, float) else ("-" if v is None else str(v))
        out.append(f"{v:>{w}}" if not isinstance(row.get(c), str) else f"{v:<{w}}")
    return "  " + "".join(out)


def _generic_table(rows: list, max_cols: int = 7) -> list:
    """Fallback for any tool without a dedicated formatter.

    This is what makes the renderer future-proof: a new tool is displayable the
    moment it is registered, without editing this file.
    """
    if not rows:
        return ["  (no rows)"]
    cols, seen = [], set()
    for r in rows:
        for k in r:
            if k not in seen and k not in ("month", "metric"):
                seen.add(k)
                cols.append(k)
    cols = cols[:max_cols]
    widths = [max(len(c) + 2, 14) for c in cols]
    out = ["  " + "".join(f"{c:>{w}}" if i else f"{c:<{w}}"
                          for i, (c, w) in enumerate(zip(cols, widths)))]
    for r in rows[:12]:
        out.append(_fmt_row(r, cols, widths))
    if len(rows) > 12:
        out.append(f"  ... {len(rows) - 12} more row(s)")
    return out


def _r_operating_metrics(rows, params):
    r = rows[0]
    return [f"  revenue {_money(r.get('revenue'))}   opex {_money(r.get('opex'))}"
            f"   operating income {_money(r.get('operating_income'))}",
            f"  headcount {r.get('total_headcount')}"
            f"   ending ARR {_money(r.get('ending_arr'))}"]


def _r_pl_summary(rows, params):
    out = [f"  {'line':<22}{'actual':>14}{'base':>14}{'variance':>14}{'OI impact':>14}"]
    for r in rows:
        out.append(f"  {r['statement_line']:<22}{_money(r['actual']):>14}"
                   f"{_money(r['base']):>14}{_money(r['variance']):>14}"
                   f"{_money(r['oi_impact']):>14}")
    return out


def _r_rank(rows, params):
    out = []
    for r in rows:
        flag = "fav" if r.get("favorable") else "UNFAV"
        share = r.get("share_of_total_oi_impact")
        pct = f"{share:.0%}" if share is not None else "-"
        out.append(f"  {r.get('rank')}. {str(r.get('name')):<28}"
                   f"{_money(r.get('oi_impact')):>14}  {flag:<6}{pct:>6} of total")
    return out


def _r_decompose(rows, params):
    out = []
    for r in rows:
        out.append(f"  {r.get('rank')}. {str(r.get('account_name')):<30}"
                   f"{_money(r.get('oi_impact')):>14}")
    return out


def _r_comp(rows, params):
    out = []
    for r in rows:
        out.append(f"  {str(r.get('department_name')):<26}"
                   f"variance {_money(r.get('salary_variance')):>12}"
                   f"   hc {_money(r.get('hc_impact')):>12}"
                   f"   rate {_money(r.get('rate_impact')):>12}")
    return out


def _r_revenue(rows, params):
    r = rows[0]
    return [f"  variance {_money(r.get('rev_variance'))}"
            f"   volume {_money(r.get('volume_impact'))}"
            f"   price {_money(r.get('price_impact'))}"]


def _r_arr(rows, params):
    r = rows[0]
    return [f"  start {_money(r.get('starting_arr'))}"
            f"  + new {_money(r.get('new_arr'))}"
            f"  + expansion {_money(r.get('expansion_arr'))}",
            f"  - contraction {_money(r.get('contraction_arr'))}"
            f"  - churn {_money(r.get('churned_arr'))}"
            f"  = end {_money(r.get('ending_arr'))}"]


def _r_headcount(rows, params):
    out = []
    for r in rows:
        out.append(f"  {str(r.get('department_name')):<26}"
                   f"actual {r.get('actual_headcount'):>4}"
                   f"   budget {r.get('budget_headcount'):>4}"
                   f"   var {r.get('hc_var_vs_budget'):>+4}")
    return out


def _r_trend(rows, params):
    metric = params.get("metric", "value")
    pts = "  ".join(f"{r['month'][:7]} {_money(r['value'])}" for r in rows[-6:])
    return [f"  {metric}: {pts}"]


def _r_periods(rows, params):
    with_act = sum(1 for r in rows if r.get("has_actuals"))
    with_fc = sum(1 for r in rows if r.get("has_forecast"))
    return [f"  {len(rows)} periods   {with_act} with actuals   {with_fc} with forecast"]


#: tool -> (heading, formatter). Headings are derived from the tool, so an
#: agent-chosen section label never changes how the section is displayed.
RENDERERS = {
    "list_periods": ("PERIOD COVERAGE", _r_periods),
    "list_dimensions": ("DIMENSION MEMBERS", None),
    "get_operating_metrics": ("HEADLINE", _r_operating_metrics),
    "get_pl_summary": ("P&L SUMMARY", _r_pl_summary),
    "rank_variance_drivers": ("TOP DRIVERS BY OPERATING-INCOME IMPACT", _r_rank),
    "decompose_variance": ("DECOMPOSITION", _r_decompose),
    "get_comp_decomposition": ("COMPENSATION: HEADCOUNT vs RATE", _r_comp),
    "get_revenue_decomposition": ("REVENUE: VOLUME vs PRICE", _r_revenue),
    "get_arr_bridge": ("ARR BRIDGE", _r_arr),
    "get_headcount_movement": ("HEADCOUNT vs PLAN", _r_headcount),
    "get_trend": ("TREND", _r_trend),
}


def _heading(tool: str, params: dict, seen: dict) -> str:
    """Disambiguate repeated tools by their distinguishing argument.

    An agent may legitimately rank twice (by department and by statement line)
    or decompose twice. Two identical headings would make the output ambiguous,
    so the varying argument is folded in.
    """
    base = RENDERERS.get(tool, (tool.replace("_", " ").upper(), None))[0]
    n = seen.get(tool, 0)
    seen[tool] = n + 1
    qualifier = params.get("dimension") or params.get("department_id") or params.get("metric")
    if n and qualifier:
        return f"{base} - {qualifier}"
    if qualifier and tool in ("rank_variance_drivers", "decompose_variance"):
        return f"{base} - {qualifier}"
    return base


def render(result) -> str:
    """Human-readable package. Every figure here came from a tool return value."""
    out = []
    add = out.append
    g = result.ledger.goal

    add("=" * 74)
    add(f" VARIANCE PACKAGE - {g['period'][:7]}  ({g['comparison']})")
    add(f" run_id {result.ledger.run_id}   outcome {result.outcome}"
        f"   complete {result.complete}")
    add("=" * 74)

    if result.outcome != "COMPLETED":
        add(f"\n  !! {result.outcome}"
            + (f" - {result.refusal_reason}" if result.refusal_reason else ""))
    if result.missing_sections:
        add(f"  !! MISSING SECTIONS: {', '.join(result.missing_sections)}")

    # Plan order, not label order: the package reads in the sequence the agent
    # actually reasoned in.
    seen: dict = {}
    for name, sec in sorted(result.sections.items(), key=lambda kv: kv[1]["step"]):
        rows, params, tool = sec["rows"], sec.get("params", {}), sec["tool"]
        if not rows:
            continue
        add("\n" + _heading(tool, params, seen))
        fmt = RENDERERS.get(tool, (None, None))[1]
        try:
            lines = fmt(rows, params) if fmt else _generic_table(rows)
        except Exception:  # noqa: BLE001
            # A formatting bug must never lose the data. Fall back to the
            # generic table rather than dropping a section that succeeded.
            lines = _generic_table(rows)
        out.extend(lines)

    if result.ledger.notes:
        add("\nNOTES")
        for n in result.ledger.notes:
            add(f"  - {n}")

    add("\n" + "-" * 74)
    add("RUN LEDGER")
    add(f"  {'#':>3} {'tool':<28}{'outcome':<14}{'rows':>5}{'ms':>8}  resolved from")
    for e in result.ledger.entries:
        src = ""
        for pname, v in e.params_declared.items():
            if isinstance(v, str) and v.startswith("$STEP_"):
                src = f"{v} -> {e.params_resolved.get(pname)}"
        add(f"  {e.step_idx:>3} {e.tool:<28}{e.outcome:<14}{e.row_count:>5}"
            f"{e.latency_ms:>8.1f}  {src}")

    c = result.ledger.cost_summary()
    add(f"\n  steps {c['steps']}   wall clock {c['wall_clock_s']}s"
        f"   tool latency {c['tool_latency_ms']}ms")
    if c.get("planner_model"):
        cost = f"${c['cost_usd']}" if c.get("pricing_known") else "unpriced"
        add(f"  planner {c['planner_model']}   planning {c['planning_latency_ms']}ms"
            f"   tokens {c['tokens_in'] + c['tokens_out']}   cost {cost}")
    else:
        add("  tokens 0   cost $0.0")
        add("  (no model in the loop: every figure above is a tool return value)")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic FP&A variance package.")
    ap.add_argument("period", help="reporting period, e.g. 2025-09")
    ap.add_argument("--comparison", default="actual_vs_budget",
                    choices=["actual_vs_budget", "actual_vs_forecast"])
    ap.add_argument("--json", action="store_true", help="emit the full run ledger as JSON")
    ap.add_argument("--narrate", action="store_true",
                    help="add deterministic commentary (no model, no key needed)")
    ap.add_argument("--no-trace", action="store_true",
                    help="hide the per-figure audit trace")
    args = ap.parse_args(argv)

    con = mz.connect_readonly()
    try:
        goal = build_goal(con, args.period, args.comparison)
    except GoalError as e:
        # Refusal is a structured terminal state, not a crash.
        print(f"REFUSED [{e.reason_code}] {e.detail}", file=sys.stderr)
        return 2

    result = Orchestrator(con).run(variance_package_plan(goal), goal)

    if args.json:
        print(result.ledger.to_json())
        return 0 if result.complete else 1

    print(render(result))

    if args.narrate:
        import run_pipeline as rp
        from agent.narrate import narrate, render_narrative
        from guardrails import entity_audit as ea

        candidate = narrate(result, goal, client=None,
                            all_entity_names=ea.canonical_entity_names(rp.load()),
                            mode="inject")
        print(render_narrative(candidate, show_trace=not args.no_trace))
        if not candidate.publishable:
            return 1

    return 0 if result.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
