"""
plan.py
=======
The plan: an ordered list of tool calls with symbolic dependencies, validated
statically before a single query runs.

WHY PLAN-THEN-EXECUTE RATHER THAN FREE-FORM ReAct
--------------------------------------------------
The whole plan is inspectable *before* anything executes. That is what a
security review wants to see, and it is what makes evaluation tractable -- you
can score the plan, not just the outcome. Free-form ReAct only tells you what
happened after it happened.

The cost is adaptivity: the planner must commit to a shape before seeing data.
Symbolic references are what buy that adaptivity back.

SYMBOLIC REFERENCES -- THE LOAD-BEARING IDEA
---------------------------------------------
A plan may declare an argument it cannot know at plan time:

    step 5: decompose_variance(period=$GOAL.period,
                               department_id=$STEP_4.rows[0].member)

The **orchestrator** resolves ``$STEP_4.rows[0].member`` from the ledger at
execution time. The model never re-reads step 4's output and re-emits a value.

This matters more than it looks. In Phase 1 the guarantee was that no tool
accepts a financial parameter. The remaining hole was behavioral: a model that
reads ``$1.24M`` from a result and retypes it into the next call has generated a
number, even though every individual guardrail held. Symbolic references close
it structurally -- **data moves between steps by reference, never by
transcription** -- and because references resolve against the ledger, the
declared and resolved forms are both recorded, so the provenance of every
argument is auditable after the fact.

Two reference roots exist:

    $GOAL.<field>              deterministic goal context (see packages.build_goal)
    $STEP_<n>.rows[<i>].<field>  a field of a prior step's result row

``$GOAL`` fields are computed in code from the dimension tables -- fiscal-year
start, prior period -- never by a model. Date arithmetic is arithmetic, and the
model has no arithmetic capability.

STATIC VALIDATION
-----------------
Before execution, ``validate_plan`` rejects: unknown tools, unknown or missing
parameters, malformed references, forward references, self references, and
references to steps that do not exist. A plan that fails validation costs zero
queries and zero tokens. This is the cheapest possible place to catch a bad
plan, and in Phase 3 it is the barrier between the model's output and the
database.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from agent.registry import REGISTRY

#: $GOAL.period  |  $STEP_4.rows[0].member
_GOAL_RE = re.compile(r"^\$GOAL\.([A-Za-z_][A-Za-z0-9_]*)$")
_STEP_RE = re.compile(r"^\$STEP_(\d+)\.rows\[(\d+)\]\.([A-Za-z_][A-Za-z0-9_]*)$")


class PlanError(ValueError):
    """Raised by validate_plan. Carries every problem, not just the first."""

    def __init__(self, problems: list):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass(frozen=True)
class GoalRef:
    field: str

    def __str__(self):
        return f"$GOAL.{self.field}"


@dataclass(frozen=True)
class StepRef:
    step_idx: int
    row: int
    field: str

    def __str__(self):
        return f"$STEP_{self.step_idx}.rows[{self.row}].{self.field}"


def parse_reference(value):
    """Return a GoalRef/StepRef, or None if the value is a literal.

    A string beginning with ``$`` that matches neither form is an *error*, not a
    literal. Silently treating a malformed reference as a literal string would
    push the failure down into parameter validation, where the error message
    would be about a nonexistent department rather than a broken plan.
    """
    if not isinstance(value, str) or not value.startswith("$"):
        return None
    m = _GOAL_RE.match(value)
    if m:
        return GoalRef(m.group(1))
    m = _STEP_RE.match(value)
    if m:
        return StepRef(int(m.group(1)), int(m.group(2)), m.group(3))
    raise PlanError([f"malformed reference '{value}'"])


@dataclass
class Step:
    idx: int
    tool: str
    params: dict = field(default_factory=dict)
    #: Optional steps may return EMPTY or fail without aborting the run. Used
    #: for enrichment (trend context) rather than package-critical sections.
    optional: bool = False
    #: Human-readable purpose. Carried into the package and the ledger so a
    #: reviewer sees intent alongside the call.
    purpose: str = ""

    def refs(self) -> list:
        out = []
        for v in self.params.values():
            r = parse_reference(v)
            if r is not None:
                out.append(r)
        return out


@dataclass
class Plan:
    goal: str
    steps: list
    #: Package sections the plan claims it will produce. Checked after
    #: execution: a plan that silently drops a section it promised is the
    #: "silent incompleteness" failure mode, and the coverage check is what
    #: makes it loud.
    promises: list = field(default_factory=list)

    def step(self, idx: int):
        for s in self.steps:
            if s.idx == idx:
                return s
        return None


def validate_plan(plan: Plan, goal_fields: set) -> None:
    """Reject a malformed plan before any query runs. Raises PlanError.

    Every problem is collected rather than failing on the first, so a replanning
    model gets one complete error instead of a sequence of them.
    """
    problems: list = []
    seen: set = set()

    if not plan.steps:
        problems.append("plan has no steps")

    for s in plan.steps:
        tag = f"step {s.idx}"

        if s.idx in seen:
            problems.append(f"{tag}: duplicate step index")
        seen.add(s.idx)

        if s.tool not in REGISTRY:
            problems.append(f"{tag}: unknown tool '{s.tool}'")
            continue
        spec = REGISTRY[s.tool]

        unknown = set(s.params) - set(spec.params)
        if unknown:
            problems.append(f"{tag}: unknown parameter(s) {sorted(unknown)} for '{s.tool}'")

        missing = [n for n, p in spec.params.items()
                   if p.required and n not in s.params]
        if missing:
            problems.append(f"{tag}: missing required parameter(s) {missing}")

        for pname, value in s.params.items():
            # No tool parameter accepts a float. A float in a plan is therefore
            # prima facie evidence that a figure was emitted where a dimension
            # member or a reference belonged. Checked here rather than only in
            # the planner so the property holds for hand-written plans too.
            if isinstance(value, float):
                problems.append(
                    f"{tag}.{pname}: numeric value {value} -- no tool parameter "
                    "accepts a figure"
                )
                continue
            try:
                ref = parse_reference(value)
            except PlanError as e:
                problems.extend(f"{tag}.{pname}: {p}" for p in e.problems)
                continue
            if ref is None:
                continue
            if isinstance(ref, GoalRef):
                if ref.field not in goal_fields:
                    problems.append(
                        f"{tag}.{pname}: '$GOAL.{ref.field}' is not a goal field "
                        f"(available: {sorted(goal_fields)})"
                    )
            else:
                if ref.step_idx == s.idx:
                    problems.append(f"{tag}.{pname}: step references itself")
                elif plan.step(ref.step_idx) is None:
                    # Checked before the forward-reference test: telling a
                    # replanning model "forward reference to step 7" when there
                    # is no step 7 sends it to fix the wrong thing.
                    problems.append(
                        f"{tag}.{pname}: references nonexistent step {ref.step_idx} "
                        f"(plan has steps {sorted(x.idx for x in plan.steps)})"
                    )
                elif ref.step_idx > s.idx:
                    problems.append(
                        f"{tag}.{pname}: forward reference to step {ref.step_idx}; "
                        "a step may only reference an earlier step"
                    )
                else:
                    problems.extend(
                        _reference_type_problems(plan, s, pname, spec.params[pname], ref)
                    )

    if problems:
        raise PlanError(problems)


def _reference_type_problems(plan: "Plan", step: "Step", pname: str,
                             pspec, ref: "StepRef") -> list:
    """Check that a reference produces the kind of thing the parameter takes.

    WHY THIS EXISTS
    ---------------
    A live planner produced this, and it passed every other check:

        step 2: rank_variance_drivers(dimension="statement_line")
        step 3: decompose_variance(department_id=$STEP_2.rows[0].member)

    Shape valid, meaning wrong. ``member`` is "Operating Expenses", which is not
    a department, so the run aborted at step 3 -- a plan that could not possibly
    have succeeded, waved through by the barrier that exists to stop exactly
    that. "Static validation is the barrier" is only worth saying if the barrier
    catches guaranteed failures.

    The larger consequence is fabrication-related. MONEY is a type no parameter
    accepts, so ``$STEP_3.rows[0].oi_impact`` is now rejected here, statically,
    with a message saying why -- rather than resolving to a float and failing
    incidentally as "not a valid department" at execution time. That closes the
    reference-shaped version of the no-figures-as-parameters rule at plan time.

    Unknown types are permitted rather than rejected: a tool that has not
    declared its field types degrades to the previous behavior instead of
    breaking every plan that references it.
    """
    from agent.registry import REGISTRY

    tag = f"step {step.idx}"
    producer = plan.step(ref.step_idx)
    if producer is None or producer.tool not in REGISTRY:
        return []

    produced = REGISTRY[producer.tool].output_type(ref.field, producer.params)
    if produced is None:
        return []   # undeclared: unknown, not wrong

    accepts = getattr(pspec, "accepts", frozenset())
    if not accepts:
        return [
            f"{tag}.{pname}: '{ref}' -- this parameter takes a literal value, "
            f"not a reference"
        ]
    if produced in accepts:
        return []

    if produced == "money":
        return [
            f"{tag}.{pname}: '{ref}' refers to a monetary field "
            f"('{ref.field}' of step {ref.step_idx}). No tool parameter accepts "
            "a figure -- reference a dimension member instead"
        ]
    return [
        f"{tag}.{pname}: '{ref}' produces {produced}, but this parameter takes "
        f"{' or '.join(sorted(accepts))}. Step {ref.step_idx} was called with "
        f"{producer.params.get('dimension', producer.params)!r}"
    ]


def resolve_params(step: Step, goal: dict, results: dict) -> dict:
    """Bind a step's declared arguments to concrete values.

    Raises ``ResolutionError`` when a reference cannot be satisfied -- most often
    because the referenced step returned fewer rows than the plan assumed. That
    is a real condition (rank 2 of a single-row result), and it must surface as
    a resolution failure the orchestrator can replan around, not as a silent
    ``None`` that becomes a confusing parameter error two layers down.
    """
    out = {}
    for pname, value in step.params.items():
        ref = parse_reference(value)
        if ref is None:
            out[pname] = value
            continue
        if isinstance(ref, GoalRef):
            if ref.field not in goal:
                raise ResolutionError(f"$GOAL.{ref.field} is not available")
            out[pname] = goal[ref.field]
            continue
        rows = results.get(ref.step_idx)
        if rows is None:
            raise ResolutionError(f"step {ref.step_idx} produced no stored result")
        if ref.row >= len(rows):
            raise ResolutionError(
                f"{ref} -- step {ref.step_idx} returned {len(rows)} row(s), "
                f"so row index {ref.row} does not exist"
            )
        row = rows[ref.row]
        if ref.field not in row:
            raise ResolutionError(
                f"{ref} -- field '{ref.field}' not in result "
                f"(available: {sorted(row)[:12]})"
            )
        out[pname] = row[ref.field]
    return out


class ResolutionError(ValueError):
    """A symbolic reference could not be satisfied at execution time."""
