"""
run_agent.py
============
Runs the full agent: a model proposes a plan, static validation gates it, the
orchestrator executes it, and the result is compared against the deterministic
baseline.

    # needs ANTHROPIC_API_KEY
    python src/agent/run_agent.py "prepare the September variance package" --period 2025-09

    # no key: shows the plan the model would have to beat
    python src/agent/run_agent.py --period 2025-09 --baseline-only

    # side by side, the comparison the whole module exists to make
    python src/agent/run_agent.py "..." --period 2025-09 --compare

THE COMPARISON IS THE POINT
---------------------------
``--compare`` runs the same goal twice: once through the hand-written
deterministic plan, once through the model. It prints both plans, both sets of
figures, and both costs.

The honest expectation is that for a fixed monthly close the deterministic
pipeline wins on every axis that matters -- same numbers, ~40ms instead of
several seconds, and $0.00 instead of a few cents. The agent earns its place
when the goal space is open, not when the goal is the same every month.

Being able to demonstrate that with measured numbers, rather than asserting that
agents are useful, is a stronger position than the agent alone.
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

from agent import materialize as mz                     # noqa: E402
from agent import tools as _tools                       # noqa: E402,F401
from agent.orchestrator import Orchestrator             # noqa: E402
from agent.packages import GoalError, build_goal, variance_package_plan  # noqa: E402
from agent.planner import (PRICING_AS_OF, PROVIDERS, Planner,  # noqa: E402
                           PlannerError, make_client, registry_fingerprint, score_plan)
from agent.run_package import render                    # noqa: E402


def show_plan(plan, title):
    print(f"\n{title}")
    print("-" * len(title))
    for s in plan.steps:
        opt = " [optional]" if s.optional else ""
        args = ", ".join(f"{k}={v}" for k, v in s.params.items())
        print(f"  {s.idx:>2}. {s.tool}({args}){opt}")
        if s.purpose:
            print(f"      -> {s.purpose}")
    if plan.promises:
        print(f"  promises: {', '.join(plan.promises)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the FP&A close-cycle agent.")
    ap.add_argument("goal", nargs="?", default="",
                    help="natural-language goal, e.g. 'prepare the September variance package'")
    ap.add_argument("--period", required=True)
    ap.add_argument("--comparison", default="actual_vs_budget",
                    choices=["actual_vs_budget", "actual_vs_forecast"])
    ap.add_argument("--provider", default="", choices=["", *PROVIDERS],
                    help="planner provider; auto-detected from your API key if omitted")
    ap.add_argument("--model", default="", help="override the provider default model")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--baseline-only", action="store_true",
                    help="run the deterministic plan only; no model, no key needed")
    ap.add_argument("--compare", action="store_true",
                    help="run both and print the cost comparison")
    ap.add_argument("--plan-only", action="store_true",
                    help="propose and validate a plan, then stop without executing")
    args = ap.parse_args(argv)

    con = mz.connect_readonly()
    try:
        goal = build_goal(con, args.period, args.comparison)
    except GoalError as e:
        print(f"REFUSED [{e.reason_code}] {e.detail}", file=sys.stderr)
        return 2

    reference = variance_package_plan(goal)

    # ---- deterministic baseline -----------------------------------------
    base = None
    if args.baseline_only or args.compare:
        base = Orchestrator(con).run(reference, goal)
        if args.baseline_only:
            show_plan(reference, "DETERMINISTIC PLAN (hand-written, no model)")
            print("\n" + render(base))
            return 0 if base.complete else 1

    # ---- planned run -----------------------------------------------------
    if not args.goal:
        print("ERROR: a goal is required unless --baseline-only is used", file=sys.stderr)
        return 2
    try:
        client = make_client(args.provider, args.model)
    except RuntimeError as e:
        print(f"ERROR: {e}\n\nUse --baseline-only to run the deterministic "
              "package with no model.", file=sys.stderr)
        return 2

    planner = Planner(client, max_attempts=args.max_attempts)
    print(f"planning with {client.model} (tool surface {registry_fingerprint()})...")
    try:
        pr = planner.propose(args.goal, goal)
    except PlannerError as e:
        print(f"\nPLANNER FAILED after {len(e.attempts)} attempt(s):", file=sys.stderr)
        for a in e.attempts:
            print(f"  attempt {a['attempt']}: {a['problems']}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        # Auth, rate limit, network, missing SDK. A raw traceback here tells the
        # user nothing actionable, and the deterministic path still works
        # without any of this, so say so.
        print(f"\nPROVIDER ERROR ({type(e).__name__}): {e}", file=sys.stderr)
        print("\nCheck the API key, the model name, and that the SDK is installed "
              f"(pip install {'openai' if 'openai' in str(type(client)).lower() else 'anthropic'}).",
              file=sys.stderr)
        print("The deterministic package needs none of these: "
              f"python src/agent/run_package.py {args.period}", file=sys.stderr)
        return 4

    show_plan(pr.plan, f"MODEL PLAN ({len(pr.attempts)} attempt(s))")
    for a in pr.attempts:
        if not a["accepted"]:
            print(f"  attempt {a['attempt']} rejected: {a['problems']}")

    print("\nPLAN QUALITY vs the deterministic reference")
    for k, v in score_plan(pr.plan, reference).items():
        print(f"  {k:<38}{v}")
    print("  (section_labels_* is a naming diff and over-reports omission;")
    print("   analysis_coverage and tools_missing_* are the meaningful signals)")

    if args.plan_only:
        return 0

    run = Orchestrator(con).run(pr.plan, goal)
    print("\n" + render(run))

    # ---- the comparison --------------------------------------------------
    if args.compare and base is not None:
        bc, ac = base.ledger.cost_summary(), run.ledger.cost_summary()
        print("\n" + "=" * 74)
        print(" DETERMINISTIC PIPELINE vs AGENT")
        print("=" * 74)
        print(f"  {'':<26}{'pipeline':>16}{'agent':>16}")
        print(f"  {'steps':<26}{bc['steps']:>16}{ac['steps']:>16}")
        print(f"  {'wall clock (s)':<26}{bc['wall_clock_s']:>16}"
              f"{round(ac['wall_clock_s'] + pr.latency_ms / 1000, 3):>16}")
        print(f"  {'planning latency (ms)':<26}{0:>16}{round(pr.latency_ms):>16}")
        print(f"  {'tokens':<26}{0:>16}{pr.tokens_in + pr.tokens_out:>16}")
        cost = f"{pr.cost_usd:.4f}" if pr.pricing_known else "unpriced"
        print(f"  {'cost (USD)':<26}{'0.0000':>16}{cost:>16}")
        print(f"  {'complete':<26}{str(base.complete):>16}{str(run.complete):>16}")

        shared = sorted(set(base.sections) & set(run.sections))
        same = [s for s in shared
                if base.sections[s]["result_hash"] == run.sections[s]["result_hash"]]
        print(f"\n  sections in common: {len(shared)}   figures identical: {len(same)}")
        if len(same) != len(shared):
            print(f"  DIVERGED: {sorted(set(shared) - set(same))}")
        print("\n  Both routed through the same tools, so identical figures are the")
        print("  expected result. What differs is the cost of getting there.")
        if pr.pricing_known:
            print(f"  Cost uses list rates as of {PRICING_AS_OF} for {client.model}; "
                  "verify before quoting.")
        else:
            print(f"  NOTE: no list price configured for '{client.model}', so cost is "
                  "reported as unpriced rather than as zero.")

    return 0 if run.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
