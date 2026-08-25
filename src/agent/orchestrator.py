"""
orchestrator.py
===============
Executes a validated plan against the bounded tool surface, records everything,
and enforces the bounds that make the run safe regardless of whether the plan
was any good.

THE GOVERNING PRINCIPLE
-----------------------
A model's *choice of action* is not verifiable against ground truth the way a
number is. There is no oracle for "should you have decomposed Sales instead of
Marketing?" So the design does not try to verify choices. It guarantees instead
that **every action the agent can take is safe to take even if it is the wrong
action**: read-only, bounded, side-effect-free, logged. A wrong choice costs a
wasted query, not a wrong decision.

What that leaves unhandled is *relevance* -- an incomplete or subtly misleading
package. Two things bound it, and neither is automation:

  * the deterministic coverage check (did the plan produce every section it
    promised), which catches silent incompleteness, and
  * the human approval gate in Phase 5.

Saying that plainly is the correct answer. A design that claimed to have
automated relevance would be overclaiming, and overclaiming is what gets caught.

ENFORCEMENT IS NOT COOPERATION
-------------------------------
Nothing here depends on the agent choosing to behave. Budgets are checked by the
orchestrator before each step; validation runs in the registry before each tool
body; the coverage check runs after execution regardless of what the plan said
it did. In Phase 4 the same principle governs the audit: the agent may *call*
``audit_narrative``, but the orchestrator gates on the result independently. A
guardrail that the agent can skip by not invoking it is not a guardrail.

NO LLM IS IMPORTED IN THIS MODULE
----------------------------------
Phase 2 runs hand-written plans end to end. That is deliberate: the execution
engine must be provably correct before a model is allowed to author the plans it
executes. It is also the eval control condition and the fallback path, so it is
built to ship quality rather than as scaffolding.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent import registry as reg
from agent.ledger import (ABORTED, BUDGET_EXHAUSTED, COMPLETED, REFUSAL_REASONS,
                          REFUSED, Budget, BudgetExhausted, LedgerEntry, RunLedger)
from agent.materialize import assert_fresh
from agent.plan import Plan, ResolutionError, resolve_params, validate_plan


@dataclass
class RunResult:
    ledger: RunLedger
    outcome: str
    sections: dict = field(default_factory=dict)
    missing_sections: list = field(default_factory=list)
    refusal_reason: str = ""

    @property
    def complete(self) -> bool:
        return self.outcome == COMPLETED and not self.missing_sections

    def summary(self) -> dict:
        return {
            "run_id": self.ledger.run_id,
            "outcome": self.outcome,
            "complete": self.complete,
            "sections": sorted(self.sections),
            "missing_sections": list(self.missing_sections),
            "refusal_reason": self.refusal_reason,
            "cost": self.ledger.cost_summary(),
        }


class Orchestrator:
    """Runs one plan. Stateless between runs; all state lives in the ledger."""

    def __init__(self, con, budget: Budget | None = None, check_freshness: bool = True):
        # Refusing to run against a stale mart is the correct behavior: stale
        # figures would pass every downstream audit while being wrong.
        if check_freshness:
            assert_fresh(con)
        self.con = con
        self.budget = budget or Budget()
        self.ctx = reg.ValidationContext(con)

    # ------------------------------------------------------------------
    def run(self, plan: Plan, goal: dict, gate0=None) -> RunResult:
        """Execute a validated plan.

        `gate0` is an optional callable receiving the validated plan and
        returning True to proceed. It runs AFTER static validation and BEFORE
        any query, so a reviewer never sees a plan that could not have run, and
        a rejected plan costs nothing. Off by default: on a read-only surface it
        adds inspectability rather than safety, and claiming otherwise would be
        overclaiming. The seam exists because a write-capable registry would
        require it.
        """
        validate_plan(plan, set(goal))          # cheapest place to fail: zero queries

        if gate0 is not None and not gate0(plan):
            from agent.gates import PlanRejected
            raise PlanRejected(
                "plan review declined the plan; no query was run"
            )

        ledger = RunLedger(goal=goal, budget=self.budget)

        outcome = COMPLETED
        refusal = ""

        for step in plan.steps:
            try:
                ledger.check_budget()
            except BudgetExhausted as e:
                ledger.notes.append(str(e))
                outcome = BUDGET_EXHAUSTED
                break

            entry, rows = self._execute(step, goal, ledger)

            if entry.outcome == reg.OK:
                continue

            if entry.outcome == reg.EMPTY:
                # A valid query with zero rows is a retrieved fact. For an
                # optional step it is enrichment that simply is not there. For a
                # required step it means a promised section cannot be produced,
                # which is a refusal -- not a prompt to try a different query.
                if step.optional:
                    ledger.notes.append(f"step {step.idx} ({step.tool}): no rows; section omitted")
                    continue
                outcome = REFUSED
                refusal = "REQUIRED_SECTION_UNAVAILABLE"
                ledger.notes.append(
                    f"step {step.idx} ({step.tool}) returned no rows and is required"
                )
                break

            # INVALID_PARAM / TOOL_ERROR / RESOLUTION_FAILED
            if step.optional:
                ledger.notes.append(f"step {step.idx} ({step.tool}): {entry.error}; section omitted")
                continue
            outcome = ABORTED
            ledger.notes.append(f"step {step.idx} ({step.tool}) failed: {entry.error}")
            break

        sections, missing = self._assemble(plan, ledger)

        if outcome == COMPLETED and missing:
            # The plan promised sections it did not produce. Surfacing this is
            # the entire point of the coverage check.
            ledger.notes.append(f"missing promised sections: {missing}")

        ledger.outcome = outcome
        ledger.refusal_reason = refusal
        ledger.finish()      # freeze the clock before anything reads it
        return RunResult(ledger, outcome, sections, missing, refusal)

    # ------------------------------------------------------------------
    def _execute(self, step, goal: dict, ledger: RunLedger):
        """Resolve, call, record. One ledger entry per attempt, always."""
        t0 = time.perf_counter()

        try:
            resolved = resolve_params(step, goal, ledger.results)
        except ResolutionError as e:
            entry = LedgerEntry(
                step_idx=step.idx, tool=step.tool,
                params_declared=dict(step.params), params_resolved={},
                outcome=reg.INVALID_PARAM,
                error=f"reference could not be resolved: {e}",
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
            return ledger.append(entry), []

        result = reg.call(step.tool, resolved, self.con, self.ctx)

        entry = LedgerEntry(
            step_idx=step.idx,
            tool=step.tool,
            params_declared=dict(step.params),
            params_resolved=dict(result.params_resolved or resolved),
            outcome=result.outcome,
            error=result.error,
            valid_alternatives=list(result.valid_alternatives),
            latency_ms=result.latency_ms,
        )
        return ledger.append(entry, rows=result.rows), result.rows

    # ------------------------------------------------------------------
    @staticmethod
    def _assemble(plan: Plan, ledger: RunLedger):
        """Collect produced sections and report promised-but-absent ones."""
        sections: dict = {}
        for step in plan.steps:
            e = ledger.entry(step.idx)
            if e is None or e.outcome != reg.OK:
                continue
            if not step.purpose:
                continue
            sections[step.purpose] = {
                "step": step.idx,
                "tool": step.tool,
                "dimension": step.params.get("dimension"),
                "params": e.params_resolved,
                "rows": ledger.results.get(step.idx, []),
                "result_hash": e.result_hash,
            }
        missing = [p for p in plan.promises if p not in sections]
        return sections, missing


def refusal_message(reason_code: str) -> str:
    return REFUSAL_REASONS.get(reason_code, reason_code)
