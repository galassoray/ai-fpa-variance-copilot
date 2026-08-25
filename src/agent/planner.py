"""
planner.py
==========
The only module in ``src/agent`` permitted to touch a model.

WHY NOT THE NATIVE TOOL-USE API
--------------------------------
The obvious implementation is native tool use: hand the model the tool schemas
and let the API loop -- model calls a tool, gets a result, calls the next one.
That is deliberately **not** what happens here.

Native tool use puts the *model* in the driver's seat: it decides at each turn
whether to call something, and the harness executes whatever comes back. The
plan exists only in retrospect, as a transcript.

This design inverts that. The model produces **one artifact -- a plan -- and
then stops.** The orchestrator executes it. That difference is the whole
architecture:

  * the complete plan is reviewable *before* any query runs (Gate 0),
  * static validation is a hard barrier between model output and the database,
  * a rejected plan costs zero queries and zero tokens,
  * plans are scoreable against a reference, so "did it plan well" is a
    measurable question rather than a vibe,
  * and replay is exact, because execution is deterministic given a plan.

The cost is one lost round of adaptivity per run: the model cannot see step 3's
result before committing to step 4. Symbolic references buy most of that back
(``$STEP_4.rows[0].member``), and the bounded replan loop handles the rest.

WHAT THE MODEL IS AND IS NOT TRUSTED WITH
------------------------------------------
Trusted with: which tools to call, in what order, with which dimension members,
and which sections the package should promise.

Not trusted with: any figure. ``validate_plan`` rejects a plan containing a
float anywhere in any parameter -- there is no tool parameter that accepts one,
so a float in a plan is prima facie evidence the model tried to emit a quantity.
Not trusted with arithmetic, SQL, execution, or publication either; it has no
capability for any of them.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

from agent.plan import Plan, PlanError, Step, validate_plan
from agent.registry import REGISTRY, tool_schemas

#: Two client implementations exist, but provider-*agnosticism* is deliberately
#: not claimed. Prompt adherence, JSON discipline, and failure modes differ
#: enough between providers that a swap is a porting job, not a config change.
#: The eval is run against one pinned configuration, and the provider and model
#: id are recorded in every result so a figure is always attributable to the
#: setup that produced it. A model version bump is a visible regression event.
DEFAULT_MODEL = "gpt-4.1"

MAX_PLAN_STEPS = 20


class PlannerError(RuntimeError):
    """The planner could not produce a valid plan within its attempt budget."""

    def __init__(self, message: str, attempts: list):
        super().__init__(message)
        self.attempts = attempts


class PlannerRefusal(RuntimeError):
    """The model declined the question because the tools cannot answer it.

    Deliberately distinct from PlannerError: failing to plan is a defect,
    declining a question outside the tool surface is correct behaviour, and
    collapsing the two would make the more impressive outcome look like a bug.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class PlannerResult:
    plan: Plan | None
    attempts: list = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    pricing_known: bool = True
    latency_ms: float = 0.0
    model: str = ""
    refusal: str = ""

    @property
    def ok(self) -> bool:
        return self.plan is not None

    @property
    def refused(self) -> bool:
        return bool(self.refusal)


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------
#: List prices per million tokens, as of the date below. Recorded here rather
#: than scattered through the code so a price change is a one-line edit with a
#: visible diff, and so any ROI figure states which rates produced it instead of
#: presenting a cost as though it were timeless.
#:
#: VERIFY BEFORE QUOTING. Published rates change, and a cost figure in an
#: interview is only as good as the rate behind it:
#:   https://platform.openai.com/docs/pricing
#:   https://www.anthropic.com/pricing
PRICING_AS_OF = "2026-08"

PRICE_PER_MTOK = {
    # OpenAI
    "gpt-4.1": {"in": 2.00, "out": 8.00},
    "gpt-4.1-mini": {"in": 0.40, "out": 1.60},
    "gpt-4o": {"in": 2.50, "out": 10.00},
    "gpt-4o-mini": {"in": 0.15, "out": 0.60},
    # Anthropic
    "claude-sonnet-4-5": {"in": 3.00, "out": 15.00},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
}


def usage_from(resp, model: str) -> dict:
    """Extract token usage and cost from a provider response.

    Handles both providers' field names. An unconfigured model reports
    ``pricing_known: False`` and a cost of None -- never a silent 0.0. A
    fabricated zero would corrupt the pipeline-vs-agent comparison this whole
    module exists to make, which is a worse failure than admitting the rate is
    unknown.
    """
    u = getattr(resp, "usage", None)
    tin = int(getattr(u, "input_tokens", None) or getattr(u, "prompt_tokens", 0) or 0)
    tout = int(getattr(u, "output_tokens", None) or getattr(u, "completion_tokens", 0) or 0)

    p = PRICE_PER_MTOK.get(model)
    if p is None:
        return {"tokens_in": tin, "tokens_out": tout,
                "cost_usd": None, "pricing_known": False}
    return {
        "tokens_in": tin,
        "tokens_out": tout,
        "cost_usd": round(tin / 1e6 * p["in"] + tout / 1e6 * p["out"], 6),
        "pricing_known": True,
    }


#: Reasoning models reject `temperature` and rename `max_tokens`. Detected by
#: prefix rather than an allow-list so a newly released one degrades gracefully
#: instead of erroring on its first call.
_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def _is_reasoning_model(model: str) -> bool:
    return any(model.startswith(p) for p in _REASONING_PREFIXES)


class OpenAIPlannerClient:
    """OpenAI planner client. Captures token usage; requests strict JSON.

    ``narrative.llm_client.OpenAIClient`` returns only text -- fine for
    commentary, insufficient here, because the agent's cost case rests on
    measured usage rather than a character-count estimate.

    Uses ``response_format={"type": "json_object"}`` where supported, which
    removes a whole class of parse failure. The strict parser stays in place
    regardless: JSON mode guarantees well-formed JSON, not a well-formed *plan*,
    and the plan is what has to be validated.
    """

    def __init__(self, model: str = "gpt-4.1", max_tokens: int = 2000,
                 temperature: float = 0.0, json_mode: bool = True):
        self.model = model
        self.max_tokens = max_tokens
        # Temperature 0: plan variance across identical goals is a measurement
        # in the eval, not something to introduce gratuitously.
        self.temperature = temperature
        self.json_mode = json_mode
        self.last_usage = {"tokens_in": 0, "tokens_out": 0,
                           "cost_usd": 0.0, "pricing_known": True}

    def _kwargs(self, system: str, user: str, json_mode: bool) -> dict:
        kw = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if _is_reasoning_model(self.model):
            kw["max_completion_tokens"] = self.max_tokens
        else:
            kw["max_tokens"] = self.max_tokens
            kw["temperature"] = self.temperature
        if json_mode:
            kw["response_format"] = {"type": "json_object"}
        return kw

    def complete(self, system: str, user: str) -> str:
        from openai import OpenAI  # lazy: offline use needs no key or SDK

        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        try:
            resp = client.chat.completions.create(**self._kwargs(system, user, self.json_mode))
        except Exception as e:  # noqa: BLE001
            # One retry without JSON mode, for models that reject the parameter.
            # Retried once and only for that reason -- a blanket retry would
            # silently mask real failures such as auth or rate limiting.
            if not self.json_mode or "response_format" not in str(e):
                raise
            self.json_mode = False
            resp = client.chat.completions.create(**self._kwargs(system, user, False))

        self.last_usage = usage_from(resp, self.model)
        return resp.choices[0].message.content or ""


class AnthropicPlannerClient:
    """Anthropic planner client. Same protocol, same usage capture."""

    def __init__(self, model: str = "claude-sonnet-4-5", max_tokens: int = 2000,
                 temperature: float = 0.0):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.last_usage = {"tokens_in": 0, "tokens_out": 0,
                           "cost_usd": 0.0, "pricing_known": True}

    def complete(self, system: str, user: str) -> str:
        import anthropic  # lazy: offline use needs no key or SDK

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            temperature=self.temperature, system=system,
            messages=[{"role": "user", "content": user}],
        )
        self.last_usage = usage_from(resp, self.model)
        return "".join(b.text for b in resp.content
                       if getattr(b, "type", None) == "text")


PROVIDERS = {
    "openai": (OpenAIPlannerClient, "gpt-4.1", "OPENAI_API_KEY"),
    "anthropic": (AnthropicPlannerClient, "claude-sonnet-4-5", "ANTHROPIC_API_KEY"),
}


def make_client(provider: str = "", model: str = ""):
    """Build a planner client, auto-detecting the provider from the environment.

    Both implementations exist, but note what is *not* being claimed: that
    plans are portable across providers. Prompt adherence, JSON discipline, and
    failure modes differ enough that a swap is a porting job. The eval is run
    against one configuration, and the provider and model id are recorded in
    every result so a figure is always attributable to the setup that produced it.
    """
    if not provider:
        for name, (_, _, env) in PROVIDERS.items():
            if os.environ.get(env):
                provider = name
                break
    if provider not in PROVIDERS:
        have = [n for n, (_, _, e) in PROVIDERS.items() if os.environ.get(e)]
        raise RuntimeError(
            "no planner provider selected. Set OPENAI_API_KEY or "
            f"ANTHROPIC_API_KEY, or pass --provider. Keys detected: {have or 'none'}"
        )
    cls, default_model, env = PROVIDERS[provider]
    if not os.environ.get(env):
        raise RuntimeError(f"provider '{provider}' selected but {env} is not set")
    return cls(model=model or default_model)


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """You are the planning component of an FP&A close-cycle agent.

You produce a PLAN. You do not execute anything, you do not write SQL, and you
do not compute or state any figure. A separate deterministic orchestrator runs
your plan and every number comes from a tool's return value.

AVAILABLE TOOLS
{tools}

PARAMETER RULES
- Every parameter value must be one of: a literal string from the tool's allowed
  values, a bounded integer, or a REFERENCE (below).
- You must NEVER supply a monetary amount, percentage, or any figure as a
  parameter. No tool accepts one. A plan containing a decimal number is rejected.
- Dimension members (department ids, account ids) must be real. If you are not
  certain a member exists, call list_dimensions first, or use a reference.

REFERENCES
A step may depend on an earlier step's result using exactly these two forms:
  $GOAL.<field>                 available fields: {goal_fields}
  $STEP_<n>.rows[<i>].<field>   a field of row <i> of step <n>'s result

References are resolved by the orchestrator at execution time. This is how you
use a value you cannot know when planning -- for example, decomposing whichever
department turns out to be the largest driver:
  {{"tool": "decompose_variance",
    "params": {{"period": "$GOAL.period",
               "department_id": "$STEP_4.rows[0].member"}}}}
Never invent a value you would otherwise have to read from a result. Use a
reference. A step may only reference a step with a LOWER index.

REFERENCE TYPES MUST MATCH
A reference must produce the KIND of value the parameter takes, and this is
checked before anything runs.
- rank_variance_drivers returns a "member" whose type follows its `dimension`
  argument. Ranked by dimension="department", member is a department id and may
  be used as a department_id. Ranked by dimension="statement_line", member is a
  statement line and CANNOT be used as a department_id.
- Never reference a monetary or percentage field (oi_impact, variance, actual,
  arr, margins). No parameter accepts a figure, so such a reference is rejected.
- Enum parameters take literal values only, never references.

IF THE TOOLS CANNOT ANSWER THE QUESTION
Some questions cannot be answered with the tools above -- cash flow, balance
sheet, pipeline, headcount by individual, anything outside this warehouse.
When that is the case, return ONLY:

  {{"refusal": "one sentence on what the question needs and why the available
  tools cannot supply it"}}

Do NOT substitute a related question you CAN answer and present it as though it
were the one asked. Refusing is correct and expected; answering a different
question is not.

OUTPUT FORMAT
Return ONLY a JSON object, no prose and no markdown fences:
{{
  "reasoning": "one or two sentences on the approach",
  "promises": ["section_name", ...],
  "steps": [
    {{"idx": 1, "tool": "tool_name", "params": {{...}},
      "purpose": "section_name", "optional": false}}
  ]
}}

- "purpose" names the package section the step produces. Steps sharing a name
  overwrite each other, so use distinct names.
- "promises" lists the sections the package must contain. If a promised section
  cannot be produced, the run refuses rather than shipping an incomplete
  package -- so promise only what your plan actually produces.
- Mark a step "optional": true only when its absence would weaken the package
  without invalidating it. Optional steps that return no rows are skipped;
  required steps that return no rows cause a refusal.
- Maximum {max_steps} steps."""

PRIOR_RUN_PROMPT = """WHAT YOU ALREADY RETRIEVED, EARLIER IN THIS SESSION

{prior}

The user is now asking a FOLLOW-UP. Plan only what the new question needs.
Do not re-run a step whose result is listed above unless the new question
genuinely requires it again -- repeating retrieval the user has already paid
for is waste, not thoroughness. You cannot reference an earlier run's rows
with $STEP_n: those belong to a finished run. If you need a value from it,
retrieve it again in this plan.
"""

USER_PROMPT = """GOAL: {goal_text}

GOAL CONTEXT (computed, authoritative -- reference these rather than restating them):
{goal_json}

Produce the plan as JSON."""

RETRY_PROMPT = """Your previous plan was rejected by static validation before
any query ran. Problems:

{problems}

Produce a corrected plan as JSON. Fix every problem listed."""


def build_system_prompt(goal_fields) -> str:
    lines = []
    for schema in tool_schemas():
        props = schema["input_schema"]["properties"]
        req = set(schema["input_schema"].get("required", []))
        params = ", ".join(
            f"{n}{'' if n in req else '?'}: "
            + (f"{'|'.join(p['enum'])}" if "enum" in p else p["type"])
            for n, p in props.items()
        ) or "(no parameters)"
        lines.append(f"- {schema['name']}({params})\n    {schema['description']}")
    return SYSTEM_PROMPT.format(
        tools="\n".join(lines),
        goal_fields=", ".join(sorted(goal_fields)),
        max_steps=MAX_PLAN_STEPS,
    )


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_plan(text: str, goal_text: str) -> Plan:
    """Turn raw model output into a Plan. Raises PlanError on anything malformed.

    Parsing is strict on purpose. Every leniency here is a way for a
    misunderstood plan to reach the validator looking well-formed -- silently
    coercing ``"idx": "3"`` to an integer, or tolerating an unknown key, means
    the plan that runs is not the plan the model wrote.
    """
    if not isinstance(text, str) or not text.strip():
        raise PlanError(["planner returned empty output"])

    cleaned = _FENCE.sub("", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise PlanError(["planner output contained no JSON object"])

    try:
        obj = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        raise PlanError([f"planner output was not valid JSON: {e}"]) from None

    if not isinstance(obj, dict):
        raise PlanError(["planner output was not a JSON object"])

    unknown = set(obj) - {"reasoning", "promises", "steps", "refusal"}
    if unknown:
        raise PlanError([f"unexpected top-level key(s): {sorted(unknown)}"])

    # A refusal is a first-class outcome, not a failure to plan. The tool
    # surface is deliberately narrow, so questions outside it are expected --
    # and answering a DIFFERENT question that the tools happen to support,
    # presented as though it were the one asked, is the worse behaviour.
    refusal = obj.get("refusal")
    if refusal and not obj.get("steps"):
        if not isinstance(refusal, str):
            raise PlanError(["'refusal' must be a string"])
        raise PlannerRefusal(refusal.strip())

    raw_steps = obj.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanError(["'steps' must be a non-empty list"])
    if len(raw_steps) > MAX_PLAN_STEPS:
        raise PlanError([f"plan has {len(raw_steps)} steps; the limit is {MAX_PLAN_STEPS}"])

    problems, steps = [], []
    for i, rs in enumerate(raw_steps, start=1):
        tag = f"step {i}"
        if not isinstance(rs, dict):
            problems.append(f"{tag}: not an object")
            continue

        extra = set(rs) - {"idx", "tool", "params", "purpose", "optional"}
        if extra:
            problems.append(f"{tag}: unexpected key(s) {sorted(extra)}")

        idx = rs.get("idx")
        if not isinstance(idx, int) or isinstance(idx, bool):
            problems.append(f"{tag}: 'idx' must be an integer")
            continue

        tool = rs.get("tool")
        if not isinstance(tool, str):
            problems.append(f"step {idx}: 'tool' must be a string")
            continue

        params = rs.get("params", {})
        if not isinstance(params, dict):
            problems.append(f"step {idx}: 'params' must be an object")
            continue

        for pname, val in params.items():
            # No tool parameter accepts a float, so a float in a plan is prima
            # facie evidence the model tried to emit a quantity. Reject here
            # rather than let it fail downstream as a type error, so the failure
            # is legible as what it actually is.
            if isinstance(val, float):
                problems.append(
                    f"step {idx}.{pname}: numeric value {val} -- no tool parameter "
                    "accepts a figure; use a literal or a reference"
                )
            elif not isinstance(val, (str, int, bool)):
                problems.append(f"step {idx}.{pname}: unsupported value type "
                                f"{type(val).__name__}")

        optional = rs.get("optional", False)
        if not isinstance(optional, bool):
            problems.append(f"step {idx}: 'optional' must be a boolean")
            optional = False

        steps.append(Step(idx=idx, tool=tool, params=params,
                          optional=optional, purpose=str(rs.get("purpose", "") or "")))

    promises = obj.get("promises", [])
    if not isinstance(promises, list) or not all(isinstance(p, str) for p in promises):
        problems.append("'promises' must be a list of strings")
        promises = []

    if problems:
        raise PlanError(problems)

    steps.sort(key=lambda s: s.idx)
    return Plan(goal=goal_text, steps=steps, promises=list(promises))


# --------------------------------------------------------------------------
# planner
# --------------------------------------------------------------------------
class Planner:
    """Proposes a validated plan, with a bounded correction loop.

    The retry loop feeds validation *problems* back to the model rather than a
    generic failure. A planner told "unknown tool 'run_sql'; valid tools are
    ..." can correct itself; one told "invalid plan" cannot, and burns its
    budget guessing.
    """

    def __init__(self, client, max_attempts: int = 3, model: str = ""):
        self.client = client
        self.max_attempts = max_attempts
        self.model = model or getattr(client, "model", "scripted")

    def propose(self, goal_text: str, goal: dict,
                prior_runs: list | None = None) -> PlannerResult:
        """Propose a plan, optionally with earlier runs from this session.

        `prior_runs` turns a one-shot planner into a conversation: the model
        sees what it already fetched and plans only the delta. That is the
        difference between a form that takes a question and an agent you can
        follow up with -- and it is bounded the same way as everything else,
        because a follow-up still produces a plan that static validation gates
        before any query runs.

        Prior rows are summarised, never replayed as $STEP references: those
        belong to a finished run whose ledger this run does not own.
        """
        system = build_system_prompt(set(goal))
        base = USER_PROMPT.format(
            goal_text=goal_text,
            goal_json=json.dumps(goal, indent=2, sort_keys=True),
        )
        user = (PRIOR_RUN_PROMPT.format(prior=summarize_prior(prior_runs))
                + "\n" + base) if prior_runs else base

        result = PlannerResult(plan=None, model=self.model)
        t0 = time.perf_counter()

        for attempt in range(1, self.max_attempts + 1):
            raw = self.client.complete(system, user)

            usage = getattr(self.client, "last_usage", None)
            if usage:
                result.tokens_in += usage.get("tokens_in", 0)
                result.tokens_out += usage.get("tokens_out", 0)
                cost = usage.get("cost_usd")
                if cost is None or not usage.get("pricing_known", True):
                    # Never accumulate a fabricated zero: an unpriced model must
                    # report "unknown", not a cost of nothing.
                    result.pricing_known = False
                else:
                    result.cost_usd += cost

            try:
                plan = parse_plan(raw, goal_text)
                validate_plan(plan, set(goal))
            except PlannerRefusal as r:
                result.refusal = r.reason
                result.attempts.append({"attempt": attempt, "accepted": False,
                                        "problems": [], "refusal": r.reason,
                                        "raw": raw[:2000]})
                result.latency_ms = (time.perf_counter() - t0) * 1000
                return result
            except PlanError as e:
                result.attempts.append({
                    "attempt": attempt, "accepted": False,
                    "problems": list(e.problems), "raw": raw[:2000],
                })
                user = (base + "\n\n" + RETRY_PROMPT.format(
                    problems="\n".join(f"- {p}" for p in e.problems)))
                continue

            result.attempts.append({"attempt": attempt, "accepted": True,
                                    "problems": [], "raw": raw[:2000]})
            result.plan = plan
            result.latency_ms = (time.perf_counter() - t0) * 1000
            return result

        result.latency_ms = (time.perf_counter() - t0) * 1000
        raise PlannerError(
            f"no valid plan after {self.max_attempts} attempts; "
            f"last problems: {result.attempts[-1]['problems']}",
            result.attempts,
        )


def summarize_prior(runs: list, max_rows: int = 3) -> str:
    """Compact digest of earlier runs in this session.

    Deliberately a SUMMARY and not the rows: handing back full results would
    grow the prompt without bound across a conversation, and would invite the
    model to quote a figure from context rather than retrieve it -- which is
    the transcription failure the whole architecture exists to prevent.
    """
    if not runs:
        return "(nothing yet)"
    out = []
    for i, r in enumerate(runs, start=1):
        goal_text = r.get("goal_text", "")
        out.append(f'Run {i} -- asked: "{goal_text}"')
        for name, sec in sorted(r.get("sections", {}).items(),
                                key=lambda kv: kv[1]["step"]):
            params = {k: v for k, v in (sec.get("params") or {}).items()
                      if k != "comparison"}
            rows = sec.get("rows") or []
            preview = ""
            if rows:
                keys = [k for k in ("name", "member", "statement_line",
                                    "account_name") if k in rows[0]]
                if keys:
                    preview = ("  e.g. " + ", ".join(
                        str(x[keys[0]]) for x in rows[:max_rows]))
            out.append(f"   {sec['tool']}({params}) -> {len(rows)} row(s){preview}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# plan scoring
# --------------------------------------------------------------------------
def score_plan(candidate: Plan, reference: Plan) -> dict:
    """Compare a proposed plan against the deterministic reference.

    Reported, not targeted. There is no oracle for "should you have decomposed
    Sales instead of Marketing?", so a low score is not necessarily a wrong
    plan -- it may be a differently reasonable one. What the score does catch is
    a planner that *omits* whole categories of analysis or that produces a
    dependency structure the reference did not need.

    Treating this as a target rather than a signal would be measuring
    conformity to one hand-written plan and calling it quality.

    READ THE TOOL METRICS, NOT THE LABEL METRICS
    Section labels are chosen by the planner. A live run produced
    "top_department_account_decomposition" for what the reference calls
    "top_driver_decomposition" -- the same analysis, a different name -- and the
    label comparison scored it as missing. So `section_labels_missing_*` is a
    naming diff and OVER-REPORTS omission; `tools_missing_vs_reference` and
    `analysis_coverage` are the signals that mean something.

    `promises_kept` / `promises_unbacked` are unaffected: those compare a plan's
    promises against its own steps, so the planner's naming is self-consistent
    by construction.
    """
    cand_tools = [s.tool for s in candidate.steps]
    ref_tools = [s.tool for s in reference.steps]
    cand_set, ref_set = set(cand_tools), set(ref_tools)

    hit = len(cand_set & ref_set)
    recall = hit / len(ref_set) if ref_set else 0.0
    precision = hit / len(cand_set) if cand_set else 0.0

    order_ok = all(
        all(r.step_idx < s.idx for r in s.refs() if hasattr(r, "step_idx"))
        for s in candidate.steps
    )

    ref_sections = {s.purpose for s in reference.steps if s.purpose}
    cand_sections = {s.purpose for s in candidate.steps if s.purpose}

    # Coverage by *analysis*, not by label: a (tool, dimension) pair identifies
    # what was actually computed, independent of what the planner called it.
    def analyses(plan):
        out = set()
        for st in plan.steps:
            dim = st.params.get("dimension")
            out.add((st.tool, dim) if isinstance(dim, str) else (st.tool, None))
        return out

    ref_an, cand_an = analyses(reference), analyses(candidate)
    coverage = len(ref_an & cand_an) / len(ref_an) if ref_an else 0.0

    return {
        "step_recall": round(recall, 3),
        "step_precision": round(precision, 3),
        "dependency_order_valid": order_ok,
        "uses_symbolic_references": any(s.refs() for s in candidate.steps),
        "n_steps": len(candidate.steps),
        "n_steps_reference": len(reference.steps),
        "tools_missing_vs_reference": sorted(ref_set - cand_set),
        "tools_extra_vs_reference": sorted(cand_set - ref_set),
        "analysis_coverage": round(coverage, 3),
        "analyses_missing_vs_reference": sorted(
            f"{t}({d})" if d else t for t, d in (ref_an - cand_an)),
        "section_labels_missing_vs_reference": sorted(ref_sections - cand_sections),
        "promises_kept": sorted(set(candidate.promises) & cand_sections),
        "promises_unbacked": sorted(set(candidate.promises) - cand_sections),
    }


def registry_fingerprint() -> str:
    """Stable identifier for the tool surface the planner was shown.

    A plan is only interpretable against the registry that produced it, so the
    eval records which surface was in play. A tool added or renamed changes this.
    """
    import hashlib

    blob = json.dumps([t.json_schema() for t in REGISTRY.values()], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
