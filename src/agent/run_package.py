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


def render(result) -> str:
    """Human-readable package. Every figure here came from a tool return value."""
    out = []
    add = out.append
    g = result.ledger.goal

    add("=" * 74)
    add(f" VARIANCE PACKAGE - {g['period']}  ({g['comparison']})")
    add(f" run_id {result.ledger.run_id}   outcome {result.outcome}"
        f"   complete {result.complete}")
    add("=" * 74)

    if result.outcome != "COMPLETED":
        add(f"\n  !! {result.outcome}"
            + (f" - {result.refusal_reason}" if result.refusal_reason else ""))
    if result.missing_sections:
        add(f"  !! MISSING SECTIONS: {', '.join(result.missing_sections)}")

    s = result.sections

    if "operating_headline" in s:
        r = s["operating_headline"]["rows"][0]
        add("\nHEADLINE")
        add(f"  revenue {_money(r['revenue'])}   opex {_money(r['opex'])}"
            f"   operating income {_money(r['operating_income'])}")
        add(f"  headcount {r['total_headcount']}   ending ARR {_money(r['ending_arr'])}")

    if "pl_summary" in s:
        add("\nP&L SUMMARY")
        add(f"  {'line':<22}{'actual':>14}{'base':>14}{'variance':>14}{'OI impact':>14}")
        for r in s["pl_summary"]["rows"]:
            add(f"  {r['statement_line']:<22}{_money(r['actual']):>14}"
                f"{_money(r['base']):>14}{_money(r['variance']):>14}"
                f"{_money(r['oi_impact']):>14}")

    if "department_drivers" in s:
        add("\nTOP DRIVERS BY OPERATING-INCOME IMPACT")
        for r in s["department_drivers"]["rows"]:
            flag = "fav" if r["favorable"] else "UNFAV"
            share = r.get("share_of_total_oi_impact")
            pct = f"{share:.0%}" if share is not None else "-"
            add(f"  {r['rank']}. {r['name']:<26}{_money(r['oi_impact']):>14}"
                f"  {flag:<6}{pct:>6} of total")

    for key, label in [("top_driver_decomposition", "DECOMPOSITION - TOP DRIVER"),
                       ("second_driver_decomposition", "DECOMPOSITION - SECOND DRIVER")]:
        if key in s:
            rows = s[key]["rows"]
            dept = rows[0].get("department_name", s[key]["params"].get("department_id"))
            add(f"\n{label}: {dept}")
            for r in rows:
                add(f"  {r['rank']}. {r['account_name']:<30}{_money(r['oi_impact']):>14}")

    if "compensation_decomposition" in s:
        add("\nCOMPENSATION: HEADCOUNT vs RATE")
        for r in s["compensation_decomposition"]["rows"]:
            add(f"  {r['department_name']:<26}variance {_money(r['salary_variance']):>12}"
                f"   hc {_money(r['hc_impact']):>12}   rate {_money(r['rate_impact']):>12}")

    if "revenue_decomposition" in s:
        r = s["revenue_decomposition"]["rows"][0]
        add("\nREVENUE: VOLUME vs PRICE")
        add(f"  variance {_money(r['rev_variance'])}"
            f"   volume {_money(r['volume_impact'])}   price {_money(r['price_impact'])}")

    if "arr_bridge" in s:
        r = s["arr_bridge"]["rows"][0]
        add("\nARR BRIDGE")
        add(f"  start {_money(r['starting_arr'])}  + new {_money(r['new_arr'])}"
            f"  + expansion {_money(r['expansion_arr'])}")
        add(f"  - contraction {_money(r['contraction_arr'])}"
            f"  - churn {_money(r['churned_arr'])}  = end {_money(r['ending_arr'])}")

    if "headcount" in s:
        add("\nHEADCOUNT vs PLAN")
        for r in s["headcount"]["rows"]:
            add(f"  {r['department_name']:<26}actual {r['actual_headcount']:>4}"
                f"   budget {r['budget_headcount']:>4}   var {r['hc_var_vs_budget']:>+4}")

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
        f"   tool latency {c['tool_latency_ms']}ms"
        f"   tokens {c['tokens_in'] + c['tokens_out']}   cost ${c['cost_usd']}")
    add("  (no model in the loop: every figure above is a tool return value)")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Deterministic FP&A variance package.")
    ap.add_argument("period", help="reporting period, e.g. 2025-09")
    ap.add_argument("--comparison", default="actual_vs_budget",
                    choices=["actual_vs_budget", "actual_vs_forecast"])
    ap.add_argument("--json", action="store_true", help="emit the full run ledger as JSON")
    args = ap.parse_args(argv)

    con = mz.connect_readonly()
    try:
        goal = build_goal(con, args.period, args.comparison)
    except GoalError as e:
        # Refusal is a structured terminal state, not a crash.
        print(f"REFUSED [{e.reason_code}] {e.detail}", file=sys.stderr)
        return 2

    result = Orchestrator(con).run(variance_package_plan(goal), goal)
    print(result.ledger.to_json() if args.json else render(result))
    return 0 if result.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
