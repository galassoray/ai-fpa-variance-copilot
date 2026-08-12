"""
registry.py
===========
The bounded tool surface. Everything the agent is permitted to do is registered
here; nothing else is reachable.

THE CENTRAL GUARANTEE
---------------------
The model chooses *which* tool to call and supplies *dimension arguments*. It
never authors computation and never supplies a financial quantity.

That second half is the non-obvious guardrail, and it is enforced structurally
rather than by instruction. A parameter's type must be one of:

    PeriodParam    -- a calendar period, validated against dim_date
    DimParam       -- a dimension member, validated against the dimension table
    EnumParam      -- one of a fixed set of flags
    IntParam       -- a bounded integer (limits, top-N)

There is deliberately **no money/float parameter type in this module.** A model
that reads ``$1.24M`` out of step 3's output and retypes it as step 4's argument
has just generated a number, inside a pipeline that otherwise never lets it.
Because no such parameter type exists, that failure is unrepresentable rather
than merely discouraged. ``test_agent_phase1.py`` asserts it over the whole
registry, so the property survives future tools written by someone who has not
read this docstring.

Data flows between steps by *reference*, not by retyping: the orchestrator
(Phase 2) resolves ``$STEP_3.rows[0].account_id`` from the run ledger and binds
it to a validated DimParam. The model sees a symbol, never a figure it must
transcribe.

OUTCOME TAXONOMY
----------------
Distinguishing these is a design requirement, not an implementation detail.
Collapsing EMPTY into an error is how agents quietly produce wrong packages: a
legitimately empty result is a *retrieved fact* ("no variance rows for that
department in that month") that the agent may narrate. It is not a signal to go
try a different query until something comes back.

    OK             rows returned
    EMPTY          valid query, zero rows -- a fact, not a failure
    INVALID_PARAM  failed validation; names the valid alternatives
    TOOL_ERROR     exception or timeout
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# --------------------------------------------------------------------------
# outcomes
# --------------------------------------------------------------------------
OK = "OK"
EMPTY = "EMPTY"
INVALID_PARAM = "INVALID_PARAM"
TOOL_ERROR = "TOOL_ERROR"

#: Hard ceiling on rows any tool may return, regardless of its own limits.
#: Bounds context cost and makes "what is the worst it can do?" answerable with
#: a number rather than an adjective.
MAX_ROWS = 200


@dataclass
class ToolResult:
    """Everything the ledger needs about one tool call."""

    tool: str
    outcome: str
    params_resolved: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)
    row_count: int = 0
    error: str = ""
    valid_alternatives: list = field(default_factory=list)
    latency_ms: float = 0.0
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome == OK

    def to_model_view(self, max_rows: int = 25) -> dict:
        """Projection handed to the model. Never the raw object.

        Truncation is *declared* rather than silent -- an agent that cannot tell
        it received a partial result will reason as though it saw everything.
        """
        view: dict[str, Any] = {"tool": self.tool, "outcome": self.outcome}
        if self.outcome == OK:
            view["row_count"] = self.row_count
            view["rows"] = self.rows[:max_rows]
            if self.row_count > max_rows:
                view["note"] = (
                    f"showing first {max_rows} of {self.row_count} rows; "
                    "narrow the query rather than assuming these are all"
                )
        elif self.outcome == EMPTY:
            view["row_count"] = 0
            view["note"] = (
                "The query was valid and returned no rows. This is a fact about "
                "the data, not an error. Do not substitute a different query to "
                "obtain rows."
            )
        else:
            view["error"] = self.error
            if self.valid_alternatives:
                view["valid_alternatives"] = self.valid_alternatives[:40]
        return view


# --------------------------------------------------------------------------
# parameter specifications
# --------------------------------------------------------------------------
class ParamSpec:
    """Base. Subclasses validate and normalize one argument."""

    json_type = "string"

    def __init__(self, description: str, required: bool = True, default: Any = None):
        self.description = description
        self.required = required
        self.default = default

    def validate(self, value: Any, ctx: "ValidationContext") -> tuple[Any, list]:
        """Return (normalized_value, valid_alternatives). Raise ValueError if invalid."""
        raise NotImplementedError

    def schema(self) -> dict:
        return {"type": self.json_type, "description": self.description}


_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$")


class PeriodParam(ParamSpec):
    """A calendar month, validated against dim_date.

    Accepts ``YYYY-MM`` and ``YYYY-MM-DD`` and normalizes to the canonical
    first-of-month key the star schema uses. Normalization is a convenience for
    the *format*; existence is still checked against the dimension, so a
    well-formed period that isn't in the data is INVALID_PARAM with the real
    list attached -- never a silent nearest-match.
    """

    def validate(self, value, ctx):
        if not isinstance(value, str):
            raise ValueError(f"period must be a string, got {type(value).__name__}")
        m = _MONTH_RE.match(value.strip())
        if not m:
            raise ValueError(f"period '{value}' is not YYYY-MM or YYYY-MM-DD")
        canonical = f"{m.group(1)}-{m.group(2)}-01"
        if canonical not in ctx.periods:
            raise ValueError(f"period '{canonical}' is not in the dataset")
        return canonical, ctx.periods

    def schema(self):
        return {
            "type": "string",
            "description": self.description + " Format: YYYY-MM (e.g. '2025-09').",
        }


class DimParam(ParamSpec):
    """A dimension member, validated against its dimension table."""

    def __init__(self, dimension: str, description: str, required: bool = True, default=None):
        super().__init__(description, required, default)
        self.dimension = dimension

    def validate(self, value, ctx):
        members = ctx.members(self.dimension)
        if not isinstance(value, str):
            raise ValueError(f"{self.dimension} must be a string, got {type(value).__name__}")
        v = value.strip()
        if v in members:
            return v, members
        # Case-insensitive rescue only; no fuzzy matching. A near-miss that
        # silently resolves is how an agent quietly reports the wrong department.
        lowered = {m.lower(): m for m in members}
        if v.lower() in lowered:
            return lowered[v.lower()], members
        raise ValueError(f"'{value}' is not a valid {self.dimension}")

    def schema(self):
        return {"type": "string", "description": self.description}


class EnumParam(ParamSpec):
    def __init__(self, choices: list, description: str, required: bool = True, default=None):
        super().__init__(description, required, default)
        self.choices = list(choices)

    def validate(self, value, ctx):
        if value not in self.choices:
            raise ValueError(f"'{value}' is not one of {self.choices}")
        return value, self.choices

    def schema(self):
        return {"type": "string", "enum": self.choices, "description": self.description}


class IntParam(ParamSpec):
    """A bounded integer. Bounds are enforced, never coerced.

    Clamping an out-of-range value would hide a planning error and let the agent
    believe it received what it asked for. The eval measures silent-coercion
    rate and it must be zero.
    """

    json_type = "integer"

    def __init__(self, lo: int, hi: int, description: str, required: bool = True, default=None):
        super().__init__(description, required, default)
        self.lo, self.hi = lo, hi

    def validate(self, value, ctx):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"expected an integer, got {type(value).__name__}")
        if not (self.lo <= value <= self.hi):
            raise ValueError(f"{value} is outside the permitted range {self.lo}..{self.hi}")
        return value, []

    def schema(self):
        return {
            "type": "integer",
            "minimum": self.lo,
            "maximum": self.hi,
            "description": self.description,
        }


#: The complete set of permitted parameter types. Asserted in tests. Adding a
#: float/money type here without a decision-log entry should fail review.
ALLOWED_PARAM_TYPES = (PeriodParam, DimParam, EnumParam, IntParam)


# --------------------------------------------------------------------------
# validation context
# --------------------------------------------------------------------------
class ValidationContext:
    """Live dimension members, read once per run from the marts.

    Validating against the *data* rather than a hardcoded list means a
    nonexistent department is a retrieved fact, and the error can hand back the
    real alternatives instead of the agent guessing again.
    """

    def __init__(self, con):
        self.con = con
        self._cache: dict[str, list] = {}

    @property
    def periods(self) -> list:
        return self.members("period")

    def members(self, dimension: str) -> list:
        if dimension in self._cache:
            return self._cache[dimension]
        sql = {
            "period": "SELECT DISTINCT month FROM dim_date ORDER BY 1",
            "department": "SELECT department_id FROM dim_department ORDER BY 1",
            "account": "SELECT account_id FROM dim_account ORDER BY 1",
            "statement_line": "SELECT DISTINCT statement_line FROM dim_account ORDER BY 1",
            "account_category": "SELECT DISTINCT account_category FROM dim_account ORDER BY 1",
        }.get(dimension)
        if sql is None:
            raise KeyError(f"unknown dimension '{dimension}'")
        vals = [str(r[0]) for r in self.con.execute(sql).fetchall()]
        self._cache[dimension] = vals
        return vals


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
@dataclass
class Tool:
    name: str
    description: str
    params: dict
    fn: Callable
    returns: str = ""

    def json_schema(self) -> dict:
        """Anthropic tool-use schema, generated from the spec.

        Generated rather than hand-written so the schema and the validator can
        never disagree -- a hand-maintained schema that drifts from the
        validator is a hole in the bounded surface.
        """
        props = {n: p.schema() for n, p in self.params.items()}
        required = [n for n, p in self.params.items() if p.required]
        desc = self.description
        if self.returns:
            desc = f"{desc} Returns: {self.returns}"
        return {
            "name": self.name,
            "description": desc,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            },
        }


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, params: dict, returns: str = ""):
    """Register a tool. Static: import-time only, no dynamic registration."""

    def deco(fn):
        for pname, spec in params.items():
            if not isinstance(spec, ALLOWED_PARAM_TYPES):
                raise TypeError(
                    f"tool '{name}' parameter '{pname}' uses {type(spec).__name__}, "
                    f"which is not a permitted parameter type. Financial quantities "
                    f"may never be tool parameters."
                )
        if name in REGISTRY:
            raise ValueError(f"tool '{name}' is already registered")
        REGISTRY[name] = Tool(name, description, params, fn, returns)
        return fn

    return deco


def tool_schemas() -> list:
    """The complete tool surface, as the model sees it."""
    return [t.json_schema() for t in REGISTRY.values()]


def call(name: str, params: dict, con, ctx: "ValidationContext | None" = None) -> ToolResult:
    """Validate and execute one tool call.

    Validation happens *here*, before the tool body runs, so every tool inherits
    it and no individual tool can forget to validate. Unknown tools and unknown
    parameters are rejected outright -- a tool surface with an escape hatch for
    "extra" arguments is not bounded.
    """
    t0 = time.perf_counter()

    if name not in REGISTRY:
        return ToolResult(
            tool=name,
            outcome=INVALID_PARAM,
            error=f"no tool named '{name}'",
            valid_alternatives=sorted(REGISTRY),
            latency_ms=(time.perf_counter() - t0) * 1000,
        )

    spec = REGISTRY[name]
    ctx = ctx or ValidationContext(con)
    params = dict(params or {})

    unknown = set(params) - set(spec.params)
    if unknown:
        return ToolResult(
            tool=name,
            outcome=INVALID_PARAM,
            error=f"unknown parameter(s) {sorted(unknown)} for tool '{name}'",
            valid_alternatives=sorted(spec.params),
            latency_ms=(time.perf_counter() - t0) * 1000,
        )

    resolved: dict = {}
    for pname, pspec in spec.params.items():
        if pname not in params or params[pname] is None:
            if pspec.required:
                return ToolResult(
                    tool=name,
                    outcome=INVALID_PARAM,
                    error=f"missing required parameter '{pname}'",
                    valid_alternatives=sorted(spec.params),
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )
            resolved[pname] = pspec.default
            continue
        try:
            value, alts = pspec.validate(params[pname], ctx)
        except ValueError as e:
            return ToolResult(
                tool=name,
                outcome=INVALID_PARAM,
                params_resolved=resolved,
                error=str(e),
                valid_alternatives=_alternatives_for(pspec, ctx),
                latency_ms=(time.perf_counter() - t0) * 1000,
            )
        resolved[pname] = value

    try:
        rows = spec.fn(con=con, **resolved)
    except Exception as e:  # noqa: BLE001 - taxonomy requires catching broadly
        return ToolResult(
            tool=name,
            outcome=TOOL_ERROR,
            params_resolved=resolved,
            error=f"{type(e).__name__}: {e}",
            latency_ms=(time.perf_counter() - t0) * 1000,
        )

    rows = list(rows or [])
    truncated = len(rows) > MAX_ROWS
    if truncated:
        rows = rows[:MAX_ROWS]

    return ToolResult(
        tool=name,
        outcome=OK if rows else EMPTY,
        params_resolved=resolved,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        latency_ms=(time.perf_counter() - t0) * 1000,
    )


def _alternatives_for(pspec: ParamSpec, ctx: ValidationContext) -> list:
    """Best available list of legal values, for the error message."""
    try:
        if isinstance(pspec, PeriodParam):
            return ctx.periods
        if isinstance(pspec, DimParam):
            return ctx.members(pspec.dimension)
        if isinstance(pspec, EnumParam):
            return pspec.choices
    except Exception:  # noqa: BLE001
        pass
    return []
