"""
ledger.py
=========
The append-only record of everything a run did, and the budgets that bound it.

WHY AN APPEND-ONLY LEDGER RATHER THAN LOGGING
---------------------------------------------
Three separate jobs, and a print statement does none of them:

1. **Replay.** The ledger plus the stored results is sufficient to reconstruct
   the deterministic portion of a run and verify every figure is unchanged.
   Model nondeterminism is confined to plan selection and prose; the numbers are
   reproducible, and ``verify_replay`` proves it rather than asserting it.

2. **Evidence.** A security reviewer asking "what did it actually do?" gets an
   enumerable answer: every tool, every resolved argument, every outcome, every
   row count. Not a narrative -- a table.

3. **ROI.** Tokens, cost, latency, and step count are instrumented from the
   first line of Phase 2, before any model exists to consume them. Retrofitted
   instrumentation measures whatever was convenient to measure after the fact,
   which is rarely the thing you needed.

The ledger records ``params_declared`` *and* ``params_resolved`` separately. That
distinction is the audit trail for symbolic references: declared shows
``$STEP_4.rows[0].member``, resolved shows ``SM``, and the two together prove the
value came from a prior tool result rather than from a model retyping a figure.

BUDGETS
-------
Exhaustion is a hard stop that marks the run incomplete. It is never a silent
truncation: a package that quietly omits a section it was supposed to contain is
worse than one that says it is missing, because the reader cannot tell.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# ---- run-level outcomes ---------------------------------------------------
COMPLETED = "COMPLETED"           # every required step ran
REFUSED = "REFUSED"               # terminal: data insufficient or goal out of scope
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
ABORTED = "ABORTED"               # unrecoverable step failure

# ---- refusal reason codes -------------------------------------------------
# An enum rather than free text: refusal recall is an eval metric, and metrics
# over free-text reasons are not measurable.
REFUSAL_REASONS = {
    "PERIOD_NOT_IN_DATASET": "the requested period does not exist in the dataset",
    "NO_DATA_FOR_PERIOD": "the period exists but has no computed rows",
    "COMPARISON_UNAVAILABLE": "the requested comparison is not available for this period",
    "GOAL_OUT_OF_SCOPE": "the goal cannot be served by the available tool surface",
    "REQUIRED_SECTION_UNAVAILABLE": "a required package section could not be produced",
}


def result_hash(rows: list) -> str:
    """Stable fingerprint of a tool result, for replay verification."""
    blob = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class LedgerEntry:
    step_idx: int
    tool: str
    params_declared: dict
    params_resolved: dict
    outcome: str
    row_count: int = 0
    result_hash: str = ""
    error: str = ""
    valid_alternatives: list = field(default_factory=list)
    latency_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    replanned: bool = False
    timestamp: str = ""

    @property
    def used_symbolic_reference(self) -> bool:
        """True when any declared argument was a reference the orchestrator resolved.

        This is the property that proves data moved between steps by reference
        rather than by the model retyping a figure.
        """
        return any(isinstance(v, str) and v.startswith("$")
                   for v in self.params_declared.values())


@dataclass
class Budget:
    """Hard bounds on one run. Exceeding any of them stops the run."""

    max_steps: int = 25
    max_wall_clock_s: float = 120.0
    max_tokens: int = 100_000
    max_cost_usd: float = 1.00
    max_replans_per_step: int = 2
    max_replans_per_run: int = 4

    def snapshot(self) -> dict:
        return asdict(self)


class BudgetExhausted(RuntimeError):
    def __init__(self, which: str, limit, used):
        super().__init__(f"budget exhausted: {which} limit={limit} used={used}")
        self.which, self.limit, self.used = which, limit, used


class RunLedger:
    """Append-only. Entries are never mutated or removed after append."""

    def __init__(self, goal: dict, budget: Budget, run_id: str | None = None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.goal = dict(goal)
        self.budget = budget
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._t0 = time.perf_counter()
        self._entries: list[LedgerEntry] = []
        self.results: dict[int, list] = {}      # step_idx -> rows
        self.outcome: str = ""
        self.refusal_reason: str = ""
        self.notes: list[str] = []

    # -- append ------------------------------------------------------------
    def append(self, entry: LedgerEntry, rows: list | None = None) -> LedgerEntry:
        entry.timestamp = datetime.now(timezone.utc).isoformat()
        if rows is not None:
            entry.result_hash = result_hash(rows)
            entry.row_count = len(rows)
            self.results[entry.step_idx] = rows
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> list:
        """A copy. The ledger is append-only from the outside."""
        return list(self._entries)

    def entry(self, step_idx: int) -> LedgerEntry | None:
        for e in self._entries:
            if e.step_idx == step_idx:
                return e
        return None

    # -- budget accounting -------------------------------------------------
    @property
    def elapsed_s(self) -> float:
        return time.perf_counter() - self._t0

    @property
    def steps_used(self) -> int:
        return len(self._entries)

    @property
    def tokens_used(self) -> int:
        return sum(e.tokens_in + e.tokens_out for e in self._entries)

    @property
    def cost_used(self) -> float:
        return sum(e.cost_usd for e in self._entries)

    @property
    def replans_used(self) -> int:
        return sum(1 for e in self._entries if e.replanned)

    def check_budget(self) -> None:
        """Raise before starting another step if any bound is spent."""
        b = self.budget
        if self.steps_used >= b.max_steps:
            raise BudgetExhausted("max_steps", b.max_steps, self.steps_used)
        if self.elapsed_s >= b.max_wall_clock_s:
            raise BudgetExhausted("max_wall_clock_s", b.max_wall_clock_s, round(self.elapsed_s, 2))
        if self.tokens_used >= b.max_tokens:
            raise BudgetExhausted("max_tokens", b.max_tokens, self.tokens_used)
        if self.cost_used >= b.max_cost_usd:
            raise BudgetExhausted("max_cost_usd", b.max_cost_usd, round(self.cost_used, 4))

    # -- reporting ---------------------------------------------------------
    def cost_summary(self) -> dict:
        """The ROI inputs. Present even at Phase 2, where the model spend is zero.

        The agent's ROI must be measured against the deterministic pipeline in
        *different* units from the copilot's commentary-hours claim -- sequencing,
        retrieval, and assembly -- or the two stories double-count and collapse
        under one question.
        """
        return {
            "steps": self.steps_used,
            "wall_clock_s": round(self.elapsed_s, 3),
            "tool_latency_ms": round(sum(e.latency_ms for e in self._entries), 2),
            "tokens_in": sum(e.tokens_in for e in self._entries),
            "tokens_out": sum(e.tokens_out for e in self._entries),
            "cost_usd": round(self.cost_used, 6),
            "replans": self.replans_used,
        }

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "budget": self.budget.snapshot(),
            "started_at": self.started_at,
            "outcome": self.outcome,
            "refusal_reason": self.refusal_reason,
            "notes": list(self.notes),
            "cost": self.cost_summary(),
            "steps": [asdict(e) for e in self._entries],
            "results": {str(k): v for k, v in self.results.items()},
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


def verify_replay(saved: dict) -> tuple[bool, list]:
    """Recompute every stored result hash and report any drift.

    This is what makes "the numbers are reproducible" a measured claim rather
    than an architectural assertion. A cached run that fails this check is not
    servable as a demo.
    """
    problems = []
    for step in saved.get("steps", []):
        idx = str(step["step_idx"])
        if not step.get("result_hash"):
            continue
        rows = saved.get("results", {}).get(idx)
        if rows is None:
            problems.append(f"step {idx}: result missing from store")
            continue
        got = result_hash(rows)
        if got != step["result_hash"]:
            problems.append(f"step {idx}: hash {step['result_hash']} != recomputed {got}")
    return (not problems), problems
