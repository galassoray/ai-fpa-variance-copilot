"""
build_demo_runs.py
==================
Regenerates the cached agent runs that the deployed app serves with no API key.

    python src/agent/build_demo_runs.py

WHY THESE ARE COMMITTED
-----------------------
The public Streamlit link has no credential. Without saved runs the agent page
would be a description of a thing rather than the thing, which is exactly the
failure the rest of this portfolio avoids.

Every run here is deterministic: hand-written plan, no model, seeded synthetic
data. Re-running this script on the same inputs reproduces byte-identical
figures, and ``replay.load_run`` verifies each artifact's hashes before the app
will display it. So the cached runs are not screenshots -- they are real runs
whose integrity is checkable.

Deliberately NOT committed: a live model run. Its plan and cost vary between
executions, so a saved one would be a snapshot of one sample presented as
typical. Live behaviour is demonstrated live, with a key, or not claimed.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, ".."))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import run_pipeline as rp                       # noqa: E402
from agent import materialize as mz             # noqa: E402
from agent import tools as _tools               # noqa: E402,F401
from agent.gates import PublicationPacket, review_plan  # noqa: E402
from agent.narrate import narrate               # noqa: E402
from agent.orchestrator import Orchestrator     # noqa: E402
from agent.packages import build_goal, variance_package_plan  # noqa: E402
from agent.replay import RUN_STORE, save_run    # noqa: E402
from guardrails import entity_audit as ea       # noqa: E402

#: One recent month as the headline demo, one early month to show the tool
#: handling a period with no trailing-twelve-month retention, and one where the
#: driver ranking differs so the symbolic binding visibly follows the data.
DEMO_PERIODS = ["2025-09", "2025-03", "2024-06"]


def build(verbose: bool = True) -> list:
    mz.materialize(verbose=False)
    con = mz.connect_readonly()
    names = ea.canonical_entity_names(rp.load())
    written = []

    try:
        for period in DEMO_PERIODS:
            goal = build_goal(con, period)
            plan = variance_package_plan(goal)

            # Gate 0 is recorded as cleared, so the cached artifact shows the
            # review step rather than implying it was skipped.
            review = review_plan(plan, reviewer="deterministic build",
                                 note="hand-written canonical plan")

            result = Orchestrator(con).run(plan, goal)
            candidate = narrate(result, goal, client=None,
                                all_entity_names=names, mode="inject")

            packet = PublicationPacket(result, candidate, plan_review=review)
            packet.approve("deterministic build",
                           note="canonical plan, deterministic narrative, "
                                "audit clean")

            path = save_run(result, candidate, packet,
                            label=f"deterministic-{period}")
            written.append(path)
            if verbose:
                c = result.ledger.cost_summary()
                print(f"  {period}  steps={c['steps']:<3} "
                      f"figures={len(candidate.matched):<3} "
                      f"published={packet.published}  -> "
                      f"{os.path.basename(path)}")
    finally:
        con.close()
    return written


if __name__ == "__main__":
    print(f"writing demo runs to {RUN_STORE}")
    paths = build()
    print(f"\n{len(paths)} run(s) written.")
